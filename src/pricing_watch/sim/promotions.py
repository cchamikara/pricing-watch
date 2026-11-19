"""Promotions in force: read by demand, the executor and the policy rules (P4)."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pricing_watch.models import ScheduledDiscount


def active_discounts(s: Session, t: datetime) -> dict[str, int]:
    """SKU -> discount % in force at t. Overlapping promotions don't stack; the largest wins."""
    return dict(
        s.execute(
            select(ScheduledDiscount.sku, func.max(ScheduledDiscount.discount_pct))
            .where(ScheduledDiscount.start <= t, ScheduledDiscount.end > t)
            .group_by(ScheduledDiscount.sku)
        ).all()
    )
