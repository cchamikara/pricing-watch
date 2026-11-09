"""Competitor feed: two invented competitors that publish prices every 6 hours.

- match_minus_5: prices 5% under our current shelf price, down to its own floor (85% of list).
- weekend_sale: holds a list price at or above our starting price, then cuts 15% on weekends.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pricing_watch.models import CompetitorPrice
from pricing_watch.rng import rng_for
from pricing_watch.sim.catalogue import CatalogueItem

PUBLISH_EVERY_HOURS = 6
COVERAGE = 0.7  # share of our SKUs each competitor stocks
WEEKEND_DISCOUNT = 0.15
MATCH_FLOOR = 0.85  # match_minus_5 won't go below this share of its list price

Strategy = Literal["match_minus_5", "weekend_sale"]


@dataclass(frozen=True)
class Competitor:
    id: str
    strategy: Strategy
    skus: Mapping[str, int]  # SKU -> the competitor's list price, inc GST cents


def make_competitors(seed: int, items: Sequence[CatalogueItem]) -> list[Competitor]:
    rng = rng_for(seed, "competitors")
    competitors = []
    strategies: tuple[tuple[str, Strategy], ...] = (("c1", "match_minus_5"), ("c2", "weekend_sale"))
    for cid, strategy in strategies:
        stocked = [i for i in items if rng.random() < COVERAGE]
        list_prices = {i.sku: round(i.price_inc_gst_cents * rng.uniform(1.0, 1.1)) for i in stocked}
        competitors.append(Competitor(cid, strategy, list_prices))
    return competitors


def publish(
    competitor: Competitor,
    items: Sequence[CatalogueItem],
    our_prices: Mapping[str, int],
    t: datetime,
) -> list[CompetitorPrice]:
    """The competitor's price list at t, or [] when t isn't a publish tick."""
    if t.hour % PUBLISH_EVERY_HOURS:
        return []
    weekend = t.weekday() >= 5
    rows = []
    for item in items:
        if item.sku not in competitor.skus:
            continue
        if competitor.strategy == "match_minus_5":
            list_price = competitor.skus[item.sku]
            price = round(max(our_prices[item.sku] * 0.95, list_price * MATCH_FLOOR))
        else:
            price = round(competitor.skus[item.sku] * (1 - WEEKEND_DISCOUNT * weekend))
        rows.append(
            CompetitorPrice(
                competitor_id=competitor.id,
                sku=item.sku,
                price_inc_gst_cents=price,
                published_at=t,
                plausible=item.plausible_low_cents <= price <= item.plausible_high_cents,
            )
        )
    return rows
