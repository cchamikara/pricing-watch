"""Supplier notices: the text the agent can read, and the change the simulator applies.

A scenario schedules each notice. At `sent_hour` the text arrives as a SupplierNotice; at
`effective_hour` the world changes: the cost moves, or the SKU stops taking orders. The date and
size of the change exist only in the scenario and in the notice's wording, never in a field the
agent can read, so acting on a notice means reading it.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Self

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from pricing_watch.models import Product, StockLevel, SupplierNotice
from pricing_watch.sim.catalogue import PROFILES


class NoticeConfig(BaseModel):
    sku: str | None = None
    category: str | None = None  # a whole range
    sent_hour: int = Field(ge=0)  # hours from the scenario start
    effective_hour: int = Field(ge=0)
    text: str = Field(min_length=1)
    cost_change_pct: int | None = Field(default=None, ge=-90, le=100)  # hidden truth: this or…
    discontinued: bool = False  # …this

    @model_validator(mode="after")
    def _one_each(self) -> Self:
        if (self.sku is None) == (self.category is None):
            raise ValueError("a notice names a sku or a category")
        if self.category is not None and self.category not in PROFILES:
            raise ValueError(f"no category {self.category!r}; expected one of {sorted(PROFILES)}")
        if (self.cost_change_pct is None) == (not self.discontinued):
            raise ValueError("a notice changes the cost or discontinues, not both or neither")
        if self.effective_hour < self.sent_hour:
            raise ValueError("a notice takes effect after it is sent")
        return self


def supplier_events(
    s: Session, notices: Sequence[NoticeConfig], start: datetime, t: datetime
) -> None:
    """Send the notices due at t, and apply the changes that take effect at t."""
    for n in notices:
        if t == start + timedelta(hours=n.sent_hour):
            s.add(SupplierNotice(sku=n.sku, category=n.category, sent_at=t, text=n.text))
        if t == start + timedelta(hours=n.effective_hour):
            scope = Product.sku == n.sku if n.sku else Product.category == n.category
            for product in s.scalars(select(Product).where(scope)):
                if n.cost_change_pct is not None:
                    stock = s.get(StockLevel, product.sku)
                    if stock and stock.avg_cost_ex_gst_cents is None:  # bought at the old cost
                        stock.avg_cost_ex_gst_cents = product.cost_ex_gst_cents
                    product.cost_ex_gst_cents = (
                        product.cost_ex_gst_cents * (100 + n.cost_change_pct) // 100
                    )
                else:
                    product.discontinued_from = t
    s.flush()
