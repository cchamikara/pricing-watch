"""Trigger detector: opens an incident for each judgment call the rules repricer can't make.

Stock checks use the *observed* count (faults can make it look wrong); everything else uses the
book. Each open incident is unique by (kind, scope), and records which SKUs looked suspect (P9).
"""

from collections.abc import Mapping
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pricing_watch.models import InboundOrder, Incident, Product, SalesTick, StockLevel
from pricing_watch.money import floor_price
from pricing_watch.policy.rules import IncidentContext
from pricing_watch.sim.catalogue import PLAUSIBLE_BAND
from pricing_watch.sim.repricer import BAND_PCT, latest_prices
from pricing_watch.sim.stock import forecast_daily_demand

WINDOW = timedelta(days=7)
# 7-day units sold / (sold + on hand), and gross margin on ex-GST revenue, per category
SELL_THROUGH_TARGET_PCT = {"tvs": 10, "audio": 10, "accessories": 15}
MARGIN_TARGET_PCT = {"tvs": 10, "audio": 20, "accessories": 40}

Key = tuple[str, str, str | None]  # kind, category, SKU (None: the whole category)


def detect(s: Session, t: datetime, observed: Mapping[str, int]) -> list[Incident]:
    """Open incidents for what fires at t. observed: SKU -> stock as seen; missing = book."""
    inbound = dict(
        s.execute(
            select(InboundOrder.sku, func.sum(InboundOrder.qty)).group_by(InboundOrder.sku)
        ).all()
    )
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
        if seen + inbound.get(p.sku, 0) < forecast * p.lead_time_hours / 24:
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

    open_keys = {
        (i.kind, i.category, i.sku)
        for i in s.scalars(select(Incident).where(Incident.closed_at.is_(None)))
    }
    category_of = dict(s.execute(select(Product.sku, Product.category)).all())
    opened = []
    for kind, category, sku in fired:
        if (kind, category, sku) in open_keys:
            continue
        in_scope = {sku} if sku else {k for k, c in category_of.items() if c == category}
        opened.append(Incident(kind=kind, category=category, sku=sku, opened_at=t,
                               suspect_skus=sorted(suspect & in_scope), closed_at=None,
                               trace_id="", session_id="", answer_source=""))  # fmt: skip
    s.add_all(opened)
    s.flush()
    return opened


def incident_context(s: Session, incident: Incident) -> IncidentContext:
    """Server-side scope and suspect set for an incident's proposals (P3 and P9)."""
    if incident.sku is not None:
        scope = frozenset({incident.sku})
    else:
        scope = frozenset(
            s.scalars(select(Product.sku).where(Product.category == incident.category))
        )
    return IncidentContext(scope=scope, suspect=frozenset(incident.suspect_skus))
