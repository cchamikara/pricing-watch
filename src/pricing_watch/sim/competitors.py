"""Competitor feed: two invented competitors that publish prices every 6 hours.

- match_minus_5: prices 5% under our current shelf price, down to its own floor (85% of list).
- weekend_sale: holds a list price at or above our starting price, then cuts 15% on weekends.

Each price carries the listing's free-text note. Weekend-sale notes tell the truth (the sale ends
on Sunday and the price is back at list on Monday) but in messy, varied wording, and some sale
prices have no note at all, so reading them helps without being a sure thing.
"""

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from pricing_watch.models import CompetitorPrice
from pricing_watch.rng import rng_for
from pricing_watch.sim.catalogue import CatalogueItem

PUBLISH_EVERY_HOURS = 6
COVERAGE = 0.7  # share of our SKUs each competitor stocks
WEEKEND_DISCOUNT = 0.15
MATCH_FLOOR = 0.85  # match_minus_5 won't go below this share of its list price

Strategy = Literal["match_minus_5", "weekend_sale"]

SALE_NOTES = (  # {list} is the competitor's list price, which really returns on Monday
    "Weekend sale, ends Sun",
    "WKND SALE - ends Sunday",
    "Sat & Sun only!!",
    "2-day sale, back to {list} on Mon",
    "weekend special ends sun midnight",
    "this wknd only",
    "Sale ends 11:59pm Sunday",
    "",  # some sale prices say nothing
)
EVERYDAY_NOTES = ("", "", "", "Price beat guarantee", "We beat any price", "Everyday low price")


@dataclass(frozen=True)
class Competitor:
    id: str
    strategy: Strategy
    skus: Mapping[str, int]  # SKU -> the competitor's list price, inc GST cents
    note_salt: int = 0  # seeds which wording each listing's note uses


def make_competitors(seed: int, items: Sequence[CatalogueItem]) -> list[Competitor]:
    rng = rng_for(seed, "competitors")
    competitors = []
    strategies: tuple[tuple[str, Strategy], ...] = (("c1", "match_minus_5"), ("c2", "weekend_sale"))
    for cid, strategy in strategies:
        stocked = [i for i in items if rng.random() < COVERAGE]
        list_prices = {i.sku: round(i.price_inc_gst_cents * rng.uniform(1.0, 1.1)) for i in stocked}
        competitors.append(Competitor(cid, strategy, list_prices))
    # own stream, so adding notes left the prices above unchanged
    salts = rng_for(seed, "competitor-notes")
    return [Competitor(c.id, c.strategy, c.skus, int(salts.integers(2**32))) for c in competitors]


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
                note=_note(competitor, item.sku, t, weekend),
            )
        )
    return rows


def _note(competitor: Competitor, sku: str, t: datetime, weekend: bool) -> str:
    """Same wording for a listing all weekend (or all the time, outside a sale)."""
    if competitor.strategy == "weekend_sale" and not weekend:
        return ""
    sale = competitor.strategy == "weekend_sale"
    period = (t - timedelta(days=t.weekday() - 5)).date() if sale else ""  # the sale's Saturday
    key = f"{competitor.note_salt}|{competitor.id}|{sku}|{period}".encode()
    notes = SALE_NOTES if sale else EVERYDAY_NOTES
    note = notes[int(hashlib.sha256(key).hexdigest()[:8], 16) % len(notes)]
    return note.format(list=f"${competitor.skus[sku] / 100:,.0f}")
