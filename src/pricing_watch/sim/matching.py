"""Matching competitor listings to our products.

The rules matcher trusts a manufacturer part number, then falls back to fuzzy title similarity.
Title similarity is a weak signal: a near-miss such as another screen size scores higher than
genuine listings padded with marketing words, so a wrong match can fake an undercut.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from pricing_watch.models import CompetitorListing, Product

FUZZY_MIN = 0.8  # the rules accept a title match this similar, or better
CANDIDATE_MIN = 0.5  # less similar than this to everything: a product we don't sell


@dataclass(frozen=True)
class Ours:
    sku: str
    title: str
    mpn: str


def normalise(title: str) -> str:
    """Lower case, units spelled one way, numbers apart from letters ('75in' is '75 in')."""
    words = title.lower().replace('"', " in ").replace("inch", "in").replace("television", "tv")
    words = re.sub(r"(\d)([a-z])", r"\1 \2", words)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", words).split())


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalise(a), normalise(b)).ratio()


def rules_match(title: str, mpn: str | None, ours: Sequence[Ours]) -> tuple[str | None, float]:
    """(SKU, confidence). A part number decides outright; otherwise the most similar title,
    if it clears FUZZY_MIN."""
    if mpn:
        return next(((o.sku, 1.0) for o in ours if o.mpn == mpn), (None, 0.0))
    scored = [(similarity(title, o.title), o.sku) for o in ours]
    best, sku = max(scored, default=(0.0, ""))
    return (sku, best) if best >= FUZZY_MIN else (None, best)


def candidates(s: Session, title: str, n: int = 3) -> list[tuple[float, Product]]:
    """Our products most like the title, best first, if at least CANDIDATE_MIN similar."""
    scored = [
        (similarity(title, p.title), p) for p in s.scalars(select(Product).order_by(Product.sku))
    ]
    best = sorted(scored, key=lambda sp: -sp[0])[:n]
    return [(score, p) for score, p in best if score >= CANDIDATE_MIN]


def ensure_listing(
    s: Session,
    competitor_id: str,
    code: str,
    title: str,
    mpn: str | None,
    true_sku: str | None,
    *,
    title_matching: bool = True,
) -> CompetitorListing:
    """The listing, matched by the rules the first time it is seen. Without title_matching only
    a part number matches; a title-only listing waits for the agent to judge it."""  # fmt: skip
    listing = s.scalars(
        select(CompetitorListing).where(CompetitorListing.competitor_id == competitor_id,
                                        CompetitorListing.listing_code == code)
    ).first()  # fmt: skip
    if listing is None:
        ours = [Ours(p.sku, p.title, p.mpn) for p in s.scalars(select(Product))]
        sku, confidence = rules_match(title, mpn, ours)
        if not title_matching and not mpn:
            sku = None
        listing = CompetitorListing(
            competitor_id=competitor_id, listing_code=code, title=title, mpn=mpn,
            matched_sku=sku, match_confidence=confidence, matched_by="rules" if sku else "",
            true_sku=true_sku,
        )  # fmt: skip
        s.add(listing)
        s.flush()
    return listing
