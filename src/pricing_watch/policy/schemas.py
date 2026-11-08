"""Argument schemas for every action tool (rule P2). Anything not listed here is not a tool."""

from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

Sku = Annotated[str, Field(min_length=1, max_length=32)]
Cents = Annotated[StrictInt, Field(gt=0)]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PriceChange(_Args):
    sku: Sku
    new_price_inc_gst_cents: Cents


class Promotion(_Args):
    skus: list[Sku] = Field(min_length=1, max_length=60)
    discount_pct: Annotated[StrictInt, Field(ge=1, le=90)]
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class Reorder(_Args):
    sku: Sku
    qty: Annotated[StrictInt, Field(ge=1, le=10_000)]


class RequestReview(_Args):
    sku: Sku
    reason: str = Field(min_length=1, max_length=500)


class NotifyMerchandiser(_Args):
    severity: Literal["info", "warning", "critical"]
    message: str = Field(min_length=1, max_length=2000)


ACTION_SCHEMAS: dict[str, type[_Args]] = {
    "propose_price_change": PriceChange,
    "propose_promotion": Promotion,
    "propose_reorder": Reorder,
    "request_review": RequestReview,
    "notify_merchandiser": NotifyMerchandiser,
}


def parse_action(tool: str, args: dict[str, Any]) -> _Args:
    """Validate arguments for a tool. Raises ValueError for unknown tools or bad arguments."""
    if tool not in ACTION_SCHEMAS:
        raise ValueError(f"unknown tool {tool!r}")
    return ACTION_SCHEMAS[tool].model_validate(args)
