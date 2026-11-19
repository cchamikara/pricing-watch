"""Argument schemas for every action tool (rule P2). Anything not listed here is not a tool."""

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, NaiveDatetime, StrictInt, model_validator

Sku = Annotated[str, Field(min_length=1, max_length=32)]
Cents = Annotated[StrictInt, Field(gt=0)]
Severity = Literal["info", "warn", "action_needed"]  # the spec's model output values


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PriceChange(_Args):
    sku: Sku
    new_price_inc_gst_cents: Cents


class Promotion(_Args):
    skus: list[Sku] = Field(min_length=1, max_length=60)
    discount_pct: Annotated[StrictInt, Field(ge=1, le=90)]
    start: NaiveDatetime  # sim times have no time zone; aware ones would crash comparisons
    end: NaiveDatetime

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


class MatchListing(_Args):
    listing_id: Annotated[StrictInt, Field(gt=0)]
    sku: Sku | None  # None: the listing is not a product we sell


class NotifyMerchandiser(_Args):
    severity: Severity
    message: str = Field(min_length=1, max_length=2000)


ACTION_SCHEMAS: dict[str, type[_Args]] = {
    "propose_price_change": PriceChange,
    "propose_promotion": Promotion,
    "propose_reorder": Reorder,
    "request_review": RequestReview,
    "match_listing": MatchListing,
    "notify_merchandiser": NotifyMerchandiser,
}


def parse_action(tool: str, args: dict[str, Any]) -> _Args:
    """Validate arguments for a tool. Raises ValueError for unknown tools or bad arguments."""
    if tool not in ACTION_SCHEMAS:
        raise ValueError(f"unknown tool {tool!r}")
    return ACTION_SCHEMAS[tool].model_validate(args)
