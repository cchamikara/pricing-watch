"""Wire the simulator together and run one scenario, in rules mode or with the agent too."""

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pricing_watch.agent.loop import handle_incident
from pricing_watch.agent.model import ClaudeProvider, GeminiProvider, ModelProvider
from pricing_watch.agent.standin import StandInModel
from pricing_watch.agent.triggers import awaiting_agent, detect
from pricing_watch.approvals.queue import scripted_review
from pricing_watch.clock import TickClock
from pricing_watch.models import (
    Action,
    CompetitorListing,
    CompetitorPrice,
    InboundOrder,
    Incident,
    Product,
    SalesTick,
    StockLevel,
    SupplierNotice,
    make_engine,
)
from pricing_watch.money import Cents, discounted, ex_from_inc
from pricing_watch.policy.config import PolicyConfig, load_policy
from pricing_watch.policy.decide import propose
from pricing_watch.policy.rules import DbStateView, IncidentContext
from pricing_watch.rng import rng_for
from pricing_watch.sim.catalogue import generate_catalogue
from pricing_watch.sim.competitors import listing_for, make_competitors, publish
from pricing_watch.sim.demand import draw_sales, hourly_rate
from pricing_watch.sim.faults import apply_feed_faults, observed_stock
from pricing_watch.sim.keywords import cost_rise_pct, discontinued_skus, stock_up_qty
from pricing_watch.sim.loop import run
from pricing_watch.sim.matching import ensure_listing
from pricing_watch.sim.promotions import active_discounts
from pricing_watch.sim.repricer import dropped_skus, execute, reprice
from pricing_watch.sim.scenario import ScenarioConfig
from pricing_watch.sim.stock import days_of_cover, forecast_daily_demand, receive_arrivals, reorder
from pricing_watch.sim.suppliers import supplier_events
from pricing_watch.tracing import NoopTracer, Tracer

OPENING_STOCK_DAYS = 14  # beyond the lead time, so the first reorder lands before running out
REORDER_COVER_DAYS = 14  # order this many days of forecast demand
SAFETY_DAYS = 7  # reorder when cover falls below lead time + this
WARMUP_DAYS = 7  # quiet rules-mode history before the start, so forecasts work from tick one
# shortcut: the agent checks for incidents once a sim day; the loop closes each incident at once,
# so an hourly check would reopen the same ones every hour. Add a cool-down to run it more often.
AGENT_HOUR = 9


@dataclass(frozen=True)
class Summary:
    gross_margin_ex_gst_cents: int
    stockout_sku_hours: int
    incidents: int
    rules_fallbacks: int  # incidents the rules plan answered because the model couldn't
    listings_matched_right: int  # matched to the true product, or rightly to none of ours
    listings_matched_wrong: int  # a wrong match feeds pricing a price for another product
    listings_missed: int  # one of ours, left unmatched: its price is lost to pricing
    sell_through: dict[str, float]  # SKU -> units sold / units available
    fingerprint: str  # hash of every sales tick and executed action, for determinism checks


def run_scenario(
    config: ScenarioConfig, tracer: Tracer | None = None, db_url: str = "sqlite://"
) -> Summary:
    tracer = tracer or NoopTracer()
    policy = load_policy()
    items = generate_catalogue(config.seed, config.start)
    competitors = make_competitors(config.seed, items)
    faults = config.fault_schedule()
    demand_rng = rng_for(config.seed, "demand")
    s = Session(make_engine(db_url))
    for i in items:
        s.add(
            Product(
                sku=i.sku,
                category=i.category,
                cost_ex_gst_cents=i.cost_ex_gst_cents,
                price_inc_gst_cents=i.price_inc_gst_cents,
                margin_floor_pct=i.margin_floor_pct,
                lead_time_hours=i.lead_time_hours,
                supplier_description=i.supplier_description,
                promo_lock_until=i.promo_lock_until,
                title=i.title,
                mpn=i.mpn,
            )
        )
    s.flush()
    initial = {
        i.sku: math.ceil(i.base_daily_demand * (i.lead_time_hours / 24 + OPENING_STOCK_DAYS))
        for i in items
    }
    s.add_all(StockLevel(sku=sku, on_hand=qty) for sku, qty in initial.items())
    s.flush()

    products = {p.sku: p for p in s.scalars(select(Product))}
    stock = {st.sku: st for st in s.scalars(select(StockLevel))}
    cheapest: dict[str, int | None] = {i.sku: None for i in items}
    opening: dict[str, int] = {}  # stock on hand when the scenario window starts
    received = dict.fromkeys(initial, 0)
    stockout_hours = 0
    published = False

    competitor_by_id = {c.id: c for c in competitors}
    item_by_sku = {i.sku: i for i in items}
    true_latest: dict[tuple[str, str], int | None] = {}  # (competitor, true SKU) -> price

    def competitor_feed(t: datetime) -> None:
        nonlocal published
        ours = {sku: p.price_inc_gst_cents for sku, p in products.items()}
        rows = [r for c in competitors for r in publish(c, items, ours, t)]
        published = bool(rows)
        if not published:
            return
        rows = apply_feed_faults(rows, faults, t)
        title_matching = config.mode != "agent"
        for nm in config.near_misses:  # not a product we sell, though its title says otherwise
            if t >= config.start + timedelta(hours=nm.start_hour):
                like = item_by_sku[nm.like_sku]
                price = like.price_inc_gst_cents * nm.price_pct // 100
                code = f"{nm.competitor.upper()}-NM-{like.sku}"
                listing = ensure_listing(s, nm.competitor, code, nm.title, None, None,
                                         title_matching=title_matching)  # fmt: skip
                s.add(CompetitorPrice(
                    competitor_id=nm.competitor, listing_id=listing.id, sku=listing.matched_sku,
                    price_inc_gst_cents=price, published_at=t,
                    plausible=like.plausible_low_cents <= price <= like.plausible_high_cents,
                ))  # fmt: skip
        for r in rows:  # publish names the true product; pricing only sees what we matched
            true = r.sku or ""
            code, title, mpn = listing_for(competitor_by_id[r.competitor_id], item_by_sku[true])
            listing = ensure_listing(s, r.competitor_id, code, title, mpn, true,
                                     title_matching=title_matching)  # fmt: skip
            r.listing_id, r.sku = listing.id, listing.matched_sku
            true_latest[r.competitor_id, true] = r.price_inc_gst_cents if r.plausible else None
        s.add_all(rows)
        s.flush()
        # customers compare the real products; a wrong match fools our pricing, not them
        # shortcut: they still see the (possibly faulted) feed; split it if a scenario needs to
        for sku in cheapest:
            seen = [p for (_, true), p in true_latest.items() if true == sku and p is not None]
            cheapest[sku] = min(seen, default=None)

    def demand(t: datetime) -> None:
        nonlocal stockout_hours
        if t == config.start:
            opening.update({sku: st.on_hand for sku, st in stock.items()})
        discounts = active_discounts(s, t)
        for i in items:
            price = discounted(products[i.sku].price_inc_gst_cents, discounts.get(i.sku, 0))
            rate = hourly_rate(i, price, t, cheapest[i.sku])
            sold, lost = draw_sales(demand_rng, rate, stock[i.sku].on_hand)
            stock[i.sku].on_hand -= sold
            stockout_hours += t >= config.start and stock[i.sku].on_hand == 0
            s.add(
                SalesTick(
                    sku=i.sku,
                    sim_time=t,
                    units_sold=sold,
                    lost_units=lost,
                    revenue_ex_gst_cents=ex_from_inc(Cents(price)) * sold,
                    cogs_cents=(
                        stock[i.sku].avg_cost_ex_gst_cents  # what it cost to buy
                        or products[i.sku].cost_ex_gst_cents
                    )
                    * sold,
                )
            )
        s.flush()

    def replenishment(t: datetime) -> None:
        supplier_events(s, config.supplier_notices, config.start, t)
        if config.text_rules:
            _stock_up_before_cost_rises(s, tracer, policy, t)
        for order in receive_arrivals(s, t):
            if t >= config.start:
                received[order.sku] += order.qty
        if t.hour:
            return
        inbound = dict(
            s.execute(
                select(InboundOrder.sku, func.sum(InboundOrder.qty)).group_by(InboundOrder.sku)
            ).all()
        )
        gone = discontinued_skus(s, t) if config.text_rules else set()
        for sku, p in products.items():
            if sku in gone:
                continue
            forecast = forecast_daily_demand(s, sku, t)
            cover = days_of_cover(stock[sku].on_hand + inbound.get(sku, 0), forecast)
            if cover < p.lead_time_hours / 24 + SAFETY_DAYS:
                reorder(s, sku, math.ceil(forecast * REORDER_COVER_DAYS), t)

    def repricer(t: datetime) -> None:
        if published:
            agent_decides = (
                awaiting_agent(s, t, dropped_skus(s, t))
                if config.mode == "agent" and t >= config.start  # the agent isn't on in warm-up
                else set()
            )
            reprice(s, tracer, t, policy, text_rules=config.text_rules, defer=agent_decides)
            scripted_review(s, tracer, policy, t, approve=config.reviewer == "approve_all")

    model = _model(config)

    def triggers(t: datetime) -> None:
        if config.mode == "agent" and t >= config.start and t.hour == AGENT_HOUR:
            observed = {
                sku: observed_stock(st.on_hand, sku, faults, t) for sku, st in stock.items()
            }
            detect(s, t, observed)

    def incidents(t: datetime) -> None:
        open_ = list(s.scalars(select(Incident).where(Incident.closed_at.is_(None))))
        for incident in open_:
            handle_incident(s, tracer, model, policy, incident, t, faults)
        if open_:
            scripted_review(s, tracer, policy, t, approve=config.reviewer == "approve_all")

    run(
        TickClock(config.start - timedelta(days=WARMUP_DAYS)),
        (WARMUP_DAYS + config.days) * 24,
        {
            "competitor_feed": competitor_feed,
            "demand": demand,
            "replenishment": replenishment,
            "repricer": repricer,
            "triggers": triggers,
            "incidents": incidents,
        },
    )
    s.commit()
    return _summarise(s, config.start, opening, received, stockout_hours)


def _stock_up_before_cost_rises(
    s: Session, tracer: Tracer, policy: PolicyConfig, t: datetime
) -> None:
    """Keyword rules: on a notice saying "cost up N%", order up to STOCK_UP_DAYS of cover at
    today's cost, through policy like any rules change. Keywords can't read the date, so it
    orders as soon as the notice arrives."""
    for notice in s.scalars(select(SupplierNotice).where(SupplierNotice.sent_at == t)):
        pct = cost_rise_pct(notice.text)
        if pct is None:
            continue
        scope = Product.sku == notice.sku if notice.sku else Product.category == notice.category
        view = DbStateView(s, t)
        for sku in s.scalars(select(Product.sku).where(scope).order_by(Product.sku)):
            on_hand, inbound = view.stock(sku)
            qty = stock_up_qty(view.forecast_daily(sku), on_hand, inbound)
            if qty <= 0:
                continue
            proposal, decision = propose(
                s, tracer, view, IncidentContext(), policy, source="rules",
                tool="propose_reorder", args={"sku": sku, "qty": qty},
                reason=f"supplier notice: stock up before cost up {pct}%",
            )  # fmt: skip
            if decision.decision == "allowed":
                execute(s, tracer, proposal, t)


def compare(config: ScenarioConfig) -> dict[str, Summary]:
    """Run the scenario against every baseline, same seed. "Agent, ignoring text" is the
    like-for-like baseline for reading text: the same agent mode with a stand-in that ignores it.
    """
    variants: dict[str, dict[str, object]] = {
        "rules, ignoring text": {"mode": "rules", "model_script": [], "text_rules": False},
        "rules, keywords": {"mode": "rules", "model_script": [], "text_rules": True},
        "agent, ignoring text": {"mode": "agent", "model": "stand-in", "model_script": [],
                                 "text_rules": False},
        f"agent ({config.model})": {"mode": "agent", "text_rules": False},
    }  # fmt: skip
    return {label: run_scenario(config.model_copy(update=v)) for label, v in variants.items()}


def _model(config: ScenarioConfig) -> ModelProvider:
    if config.model == "claude":
        return ClaudeProvider()
    if config.model == "gemini":
        return GeminiProvider()
    return StandInModel(config.model_script)


def _summarise(
    s: Session,
    start: datetime,
    opening: dict[str, int],
    received: dict[str, int],
    stockout_hours: int,
) -> Summary:
    """Summarise the scenario window only; the warm-up is left out."""
    ticks = list(
        s.scalars(
            select(SalesTick)
            .where(SalesTick.sim_time >= start)
            .order_by(SalesTick.sim_time, SalesTick.sku)
        )
    )
    actions = list(s.scalars(select(Action).where(Action.executed_at >= start).order_by(Action.id)))
    h = hashlib.sha256()
    for tick in ticks:
        h.update(f"{tick.sku}|{tick.sim_time}|{tick.units_sold}|{tick.lost_units}\n".encode())
    for a in actions:
        h.update(f"{a.executed_at}|{a.tool}|{json.dumps(a.args, sort_keys=True)}\n".encode())
    sold: dict[str, int] = {}
    for tick in ticks:
        sold[tick.sku] = sold.get(tick.sku, 0) + tick.units_sold
    sources = list(s.scalars(select(Incident.answer_source)))
    listings = list(s.scalars(select(CompetitorListing)))
    return Summary(
        gross_margin_ex_gst_cents=sum(t.revenue_ex_gst_cents - t.cogs_cents for t in ticks),
        stockout_sku_hours=stockout_hours,
        incidents=len(sources),
        rules_fallbacks=sources.count("rules_fallback"),
        listings_matched_right=sum(li.matched_sku == li.true_sku for li in listings),
        listings_matched_wrong=sum(
            li.matched_sku is not None and li.matched_sku != li.true_sku for li in listings
        ),
        listings_missed=sum(li.matched_sku is None and li.true_sku is not None for li in listings),
        sell_through={
            # a SKU that sold out in the warm-up with nothing arriving had 0 units to sell
            sku: sold.get(sku, 0) / max(opening[sku] + received[sku], 1)
            for sku in sorted(opening)
        },
        fingerprint=h.hexdigest(),
    )
