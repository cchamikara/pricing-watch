"""Wire the simulator together and run one scenario in rules mode."""

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pricing_watch.approvals.queue import scripted_review
from pricing_watch.clock import TickClock
from pricing_watch.models import (
    Action,
    InboundOrder,
    Product,
    SalesTick,
    StockLevel,
    make_engine,
)
from pricing_watch.money import Cents, ex_from_inc
from pricing_watch.policy.config import load_policy
from pricing_watch.rng import rng_for
from pricing_watch.sim.catalogue import generate_catalogue
from pricing_watch.sim.competitors import make_competitors, publish
from pricing_watch.sim.demand import draw_sales, hourly_rate
from pricing_watch.sim.faults import apply_feed_faults
from pricing_watch.sim.loop import run
from pricing_watch.sim.repricer import cheapest_plausible, reprice
from pricing_watch.sim.scenario import ScenarioConfig
from pricing_watch.sim.stock import days_of_cover, forecast_daily_demand, receive_arrivals, reorder
from pricing_watch.tracing import NoopTracer, Tracer

OPENING_STOCK_DAYS = 14  # beyond the lead time, so the first reorder lands before running out
REORDER_COVER_DAYS = 14  # order this many days of forecast demand
SAFETY_DAYS = 7  # reorder when cover falls below lead time + this
WARMUP_DAYS = 7  # quiet rules-mode history before the start, so forecasts work from tick one


@dataclass(frozen=True)
class Summary:
    gross_margin_ex_gst_cents: int
    stockout_sku_hours: int
    sell_through: dict[str, float]  # SKU -> units sold / units available
    fingerprint: str  # hash of every sales tick and price change, for determinism checks


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

    def competitor_feed(t: datetime) -> None:
        nonlocal published
        ours = {sku: p.price_inc_gst_cents for sku, p in products.items()}
        rows = [r for c in competitors for r in publish(c, items, ours, t)]
        published = bool(rows)
        if published:
            s.add_all(apply_feed_faults(rows, faults, t))
            s.flush()
            # shortcut: customers react to the same (possibly faulted) feed our system sees;
            # split true and observed feeds if a scenario needs customers to ignore faults
            for sku in cheapest:
                cheapest[sku] = cheapest_plausible(s, sku)

    def demand(t: datetime) -> None:
        nonlocal stockout_hours
        if t == config.start:
            opening.update({sku: st.on_hand for sku, st in stock.items()})
        for i in items:
            price = products[i.sku].price_inc_gst_cents
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
                    cogs_cents=i.cost_ex_gst_cents * sold,
                )
            )
        s.flush()

    def replenishment(t: datetime) -> None:
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
        for sku, p in products.items():
            forecast = forecast_daily_demand(s, sku, t)
            cover = days_of_cover(stock[sku].on_hand + inbound.get(sku, 0), forecast)
            if cover < p.lead_time_hours / 24 + SAFETY_DAYS:
                reorder(s, sku, math.ceil(forecast * REORDER_COVER_DAYS), t)

    def repricer(t: datetime) -> None:
        if published:
            reprice(s, tracer, t, policy)
            scripted_review(s, tracer, policy, t, approve=config.reviewer == "approve_all")

    run(
        TickClock(config.start - timedelta(days=WARMUP_DAYS)),
        (WARMUP_DAYS + config.days) * 24,
        {
            "competitor_feed": competitor_feed,
            "demand": demand,
            "replenishment": replenishment,
            "repricer": repricer,
        },
    )
    s.commit()
    return _summarise(s, config.start, opening, received, stockout_hours)


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
        h.update(f"{a.executed_at}|{a.args['sku']}|{a.args['new_price_inc_gst_cents']}\n".encode())
    sold: dict[str, int] = {}
    for tick in ticks:
        sold[tick.sku] = sold.get(tick.sku, 0) + tick.units_sold
    return Summary(
        gross_margin_ex_gst_cents=sum(t.revenue_ex_gst_cents - t.cogs_cents for t in ticks),
        stockout_sku_hours=stockout_hours,
        sell_through={
            # a SKU that sold out in the warm-up with nothing arriving had 0 units to sell
            sku: sold.get(sku, 0) / max(opening[sku] + received[sku], 1)
            for sku in sorted(opening)
        },
        fingerprint=h.hexdigest(),
    )
