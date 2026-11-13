"""Trigger detector: opens an incident for each judgment call the rules repricer can't make.

Stock checks use the *observed* count (faults can make it look wrong); everything else uses the
book. Each (kind, scope) has at most one open incident, stays quiet for COOLDOWN after one closes,
and records which SKUs looked suspect (P9).
"""

from collections.abc import Mapping
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pricing_watch.models import (
    Action,
    CompetitorListing,
    InboundOrder,
    Incident,
    Product,
    Proposal,
    SalesTick,
    StockLevel,
    SupplierNotice,
)
from pricing_watch.money import floor_price
from pricing_watch.policy.rules import IncidentContext
from pricing_watch.sim.catalogue import PLAUSIBLE_BAND
from pricing_watch.sim.matching import candidates
from pricing_watch.sim.repricer import BAND_PCT, dropped_skus, latest_prices
from pricing_watch.sim.stock import forecast_daily_demand

WINDOW = timedelta(days=7)
COOLDOWN = timedelta(days=3)  # a closed incident's (kind, scope) stays quiet this long
# 7-day units sold / (sold + on hand), and gross margin on ex-GST revenue, per category
SELL_THROUGH_TARGET_PCT = {"tvs": 10, "audio": 10, "accessories": 15}
MARGIN_TARGET_PCT = {"tvs": 10, "audio": 20, "accessories": 40}

Key = tuple[str, str, str | None]  # kind, category, SKU (None: the whole category)


def detect(s: Session, t: datetime, observed: Mapping[str, int]) -> list[Incident]:
    """Open incidents for what fires at t. observed: SKU -> stock as seen; missing = book."""
    deliveries: dict[str, list[tuple[datetime, int]]] = {}
    for order in s.scalars(select(InboundOrder).order_by(InboundOrder.eta, InboundOrder.id)):
        deliveries.setdefault(order.sku, []).append((order.eta, order.qty))
    sales = {
        sku: (sold, revenue, cogs)
        for sku, sold, revenue, cogs in s.execute(
            select(SalesTick.sku, func.sum(SalesTick.units_sold),
                   func.sum(SalesTick.revenue_ex_gst_cents), func.sum(SalesTick.cogs_cents))
            .where(SalesTick.sim_time >= t - WINDOW, SalesTick.sim_time < t)
            .group_by(SalesTick.sku)
        )
    }  # fmt: skip
    fired: list[Key] = []
    suspect: set[str] = set()
    margin: dict[str, tuple[int, int]] = {}  # category -> (revenue, cogs)
    for p in s.scalars(select(Product).order_by(Product.sku)):
        book = s.get_one(StockLevel, p.sku).on_hand
        seen = observed.get(p.sku, book)
        sold, revenue, cogs = sales.get(p.sku, (0, 0, 0))
        rev, cg = margin.get(p.category, (0, 0))
        margin[p.category] = (rev + revenue, cg + cogs)

        feed = latest_prices(s, p.sku)
        bad_feed = any(not r.plausible for r in feed)
        if bad_feed or not PLAUSIBLE_BAND[0] * book <= seen <= PLAUSIBLE_BAND[1] * book:
            suspect.add(p.sku)
            fired.append(("suspect_feed", p.category, p.sku))
        forecast = forecast_daily_demand(s, p.sku, t)
        lead = timedelta(hours=p.lead_time_hours)
        if _runs_out(seen, deliveries.get(p.sku, []), forecast / 24, t, t + lead):
            fired.append(("stockout_risk", p.category, p.sku))
        cheapest = min((r.price_inc_gst_cents for r in feed if r.plausible), default=None)
        floor = floor_price(p.cost_ex_gst_cents, p.margin_floor_pct)
        if cheapest is not None and cheapest * 100 < floor * BAND_PCT[0]:
            fired.append(("competitor_undercut", p.category, p.sku))
        if sold * 100 < SELL_THROUGH_TARGET_PCT[p.category] * (sold + book):
            fired.append(("slow_mover", p.category, p.sku))
    for category, (revenue, cogs) in sorted(margin.items()):
        if (revenue - cogs) * 100 < MARGIN_TARGET_PCT[category] * revenue:
            fired.append(("margin_erosion", category, None))

    fired += _notice_keys(s, t)
    category_by_sku = dict(s.execute(select(Product.sku, Product.category)).all())
    fired += [("competitor_price_drop", category_by_sku[k], k) for k in sorted(dropped_skus(s, t))]
    quiet = (Incident.kind != "supplier_notice") & (
        Incident.closed_at.is_(None) | (Incident.closed_at > t - COOLDOWN)
    )  # each notice is its own event: _notice_keys dedupes those
    open_keys = {(i.kind, i.category, i.sku) for i in s.scalars(select(Incident).where(quiet))}
    category_of = dict(s.execute(select(Product.sku, Product.category)).all())
    opened = []
    for kind, category, sku in fired:
        if (kind, category, sku) in open_keys:
            continue
        in_scope = {sku} if sku else {k for k, c in category_of.items() if c == category}
        opened.append(Incident(kind=kind, category=category, sku=sku, opened_at=t,
                               suspect_skus=sorted(suspect & in_scope), closed_at=None,
                               trace_id="", session_id="", answer_source=""))  # fmt: skip
    opened += _listing_incidents(s, t)
    s.add_all(opened)
    s.flush()
    return opened


def _listing_incidents(s: Session, t: datetime) -> list[Incident]:
    """One listing_match incident per listing still waiting to be matched, the first time it is
    seen. Scope is the best candidate's category, so the agent may pick any SKU in it."""
    judged = select(Incident.listing_id).where(Incident.listing_id.is_not(None))
    waiting = s.scalars(
        select(CompetitorListing)
        .where(CompetitorListing.matched_by == "", CompetitorListing.id.not_in(judged))
        .order_by(CompetitorListing.id)
    )
    opened = []
    for listing in waiting:
        best = candidates(s, listing.title, n=1)
        if best:  # nothing like it: a product we don't sell, nothing to judge
            opened.append(Incident(
                kind="listing_match", category=best[0][1].category, sku=None, suspect_skus=[],
                opened_at=t, closed_at=None, trace_id="", session_id="", answer_source="",
                listing_id=listing.id,
            ))  # fmt: skip
    return opened


def _notice_keys(s: Session, t: datetime) -> list[Key]:
    """One supplier_notice incident per SKU a notice covers, for each notice that arrived after
    that SKU's last one, so every notice is handled once whatever the cool-down."""
    last = dict(
        s.execute(
            select(Incident.sku, func.max(Incident.opened_at))
            .where(Incident.kind == "supplier_notice")
            .group_by(Incident.sku)
        ).all()
    )
    category_of = dict(s.execute(select(Product.sku, Product.category)).all())
    keys: set[Key] = set()
    for n in s.scalars(select(SupplierNotice).where(SupplierNotice.sent_at <= t)):
        covered = [n.sku] if n.sku else [k for k, c in category_of.items() if c == n.category]
        for sku in covered:
            if sku in category_of and (sku not in last or n.sent_at > last[sku]):
                keys.add(("supplier_notice", category_of[sku], sku))
    return sorted(keys, key=lambda k: k[2] or "")


def _runs_out(
    on_hand: int,
    deliveries: list[tuple[datetime, int]],
    per_hour: float,
    t: datetime,
    horizon: datetime,
) -> bool:
    """Does stock run out before a delivery lands, or fall short of demand up to the horizon?

    Only deliveries that land within the horizon count, each from the hour it arrives.
    """  # fmt: skip
    stock = float(on_hand)
    for eta, qty in deliveries:  # sorted by arrival
        if eta >= horizon:
            break
        if stock < per_hour * ((eta - t) / timedelta(hours=1)):
            return True  # empty before this delivery arrives
        stock += qty
    return stock < per_hour * ((horizon - t) / timedelta(hours=1))


def awaiting_agent(s: Session, t: datetime, dropped: set[str]) -> set[str]:
    """Of the SKUs with a sharp competitor drop, those the repricer leaves to the agent: not yet
    decided, or held (no price change executed) within the cool-down. A SKU whose drop the
    agent followed goes back to the repricer."""
    waiting = set()
    for sku in dropped:
        last = s.scalars(
            select(Incident)
            .where(Incident.kind == "competitor_price_drop", Incident.sku == sku)
            .order_by(Incident.opened_at.desc(), Incident.id.desc())
            .limit(1)
        ).first()
        if last is None or last.closed_at is None:
            waiting.add(sku)
            continue
        followed = s.scalar(
            select(func.count())
            .select_from(Action)
            .join(Proposal, Proposal.id == Action.proposal_id)
            .where(Proposal.incident_id == last.id, Action.tool == "propose_price_change")
        )
        if not followed and last.closed_at > t - COOLDOWN:
            waiting.add(sku)
    return waiting


def incident_context(s: Session, incident: Incident) -> IncidentContext:
    """Server-side scope and suspect set for an incident's proposals (P3 and P9)."""
    if incident.sku is not None:
        scope = frozenset({incident.sku})
    else:
        scope = frozenset(
            s.scalars(select(Product.sku).where(Product.category == incident.category))
        )
    return IncidentContext(scope=scope, suspect=frozenset(incident.suspect_skus),
                           listing_id=incident.listing_id)  # fmt: skip
