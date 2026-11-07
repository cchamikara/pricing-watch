"""Stock, replenishment and the demand forecast the agent and repricer can see.

The forecast uses demand (sold + lost), so a stockout doesn't drag it down. It assumes the
demand hook writes a SalesTick for every SKU on every tick, including zero-sales ticks.
"""

import math
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pricing_watch.models import InboundOrder, Product, SalesTick, StockLevel

FORECAST_WINDOW = timedelta(days=7)
MIN_HISTORY_HOURS = 24


def reorder(s: Session, sku: str, qty: int, t: datetime) -> InboundOrder:
    lead = timedelta(hours=s.get_one(Product, sku).lead_time_hours)
    order = InboundOrder(sku=sku, qty=qty, eta=t + lead)
    s.add(order)
    s.flush()
    return order


def receive_arrivals(s: Session, t: datetime) -> list[InboundOrder]:
    """Move every order due by t into stock on hand."""
    due = list(s.scalars(select(InboundOrder).where(InboundOrder.eta <= t)))
    for order in due:
        s.get_one(StockLevel, order.sku).on_hand += order.qty
        s.delete(order)
    s.flush()
    return due


def forecast_daily_demand(s: Session, sku: str, t: datetime) -> float:
    """Average daily demand over the 7 days before t, or 0 (no forecast) before a full day.

    Demand is uneven across the day, so scaling a partial day up to 24 h misleads by up to 4x.
    """
    demanded, hours = s.execute(
        select(func.sum(SalesTick.units_sold + SalesTick.lost_units), func.count())
        .where(SalesTick.sku == sku)
        .where(SalesTick.sim_time >= t - FORECAST_WINDOW, SalesTick.sim_time < t)
    ).one()
    return float(demanded * 24 / hours) if hours >= MIN_HISTORY_HOURS else 0.0


def days_of_cover(on_hand: int, forecast_daily: float) -> float:
    return on_hand / forecast_daily if forecast_daily > 0 else math.inf
