"""Keyword rules for text: the fair baseline for "does reading text pay?".

Deliberately simple, the way a team would first try it: a few keywords, no understanding. It
catches tidy wording and misses abbreviations, rephrasing and split phrases; the misses are the
point, since they show what a model would have to add.
"""

import math
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from pricing_watch.models import Product, SupplierNotice

STOCK_UP_DAYS = 45  # before a cost rise, order up to this cover (policy caps it at 60)

_TEMPORARY = re.compile(r"\b(weekend|ends|clearance)\b", re.IGNORECASE)
_COST_UP = re.compile(r"\bcost up (\d+)\s*%", re.IGNORECASE)
_DISCONTINUED = re.compile(r"\bdiscontinued\b", re.IGNORECASE)


def looks_temporary(note: str) -> bool:
    return bool(_TEMPORARY.search(note))


def cost_rise_pct(text: str) -> int | None:
    match = _COST_UP.search(text)
    return int(match.group(1)) if match else None


def is_discontinued(text: str) -> bool:
    return bool(_DISCONTINUED.search(text))


def discontinued_skus(s: Session, t: datetime) -> set[str]:
    """SKUs a notice received by t calls discontinued, by keyword."""
    skus: set[str] = set()
    for n in s.scalars(select(SupplierNotice).where(SupplierNotice.sent_at <= t)):
        if is_discontinued(n.text):
            scope = Product.sku == n.sku if n.sku else Product.category == n.category
            skus.update(s.scalars(select(Product.sku).where(scope)))
    return skus


def stock_up_qty(forecast_daily: float, on_hand: int, inbound: int) -> int:
    """Units to order now, at today's cost, to hold STOCK_UP_DAYS of cover through a rise."""
    return math.ceil(forecast_daily * STOCK_UP_DAYS) - on_hand - inbound
