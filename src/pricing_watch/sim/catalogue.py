"""Seeded catalogue: 3 categories x 20 SKUs, plus the hidden demand parameters per SKU."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from pricing_watch.money import Cents, inc_from_ex
from pricing_watch.rng import rng_for

SKUS_PER_CATEGORY = 20
PLAUSIBLE_BAND = (0.5, 1.5)  # competitor prices outside this share of our shelf price are suspect
PROMO_LOCK_SHARE = 0.1


@dataclass(frozen=True)
class CategoryProfile:
    prefix: str
    markup: tuple[float, float]  # shelf price ex GST / cost
    floor_pct: tuple[int, int]
    lead_hours: tuple[int, int]
    elasticity: tuple[float, float]
    daily_demand: tuple[float, float]
    products: tuple[tuple[str, tuple[float, float]], ...]  # (name, cost range in dollars)


PROFILES = {
    # TVs: thin margins, price-sensitive, slow, imported. Keep min markup above max floor.
    "tvs": CategoryProfile(
        "TV",
        (1.15, 1.30),
        (5, 10),
        (336, 672),
        (-3.5, -2.0),
        (0.3, 3),
        (('55" 4K TV', (400, 900)), ('65" OLED TV', (1500, 3000)), ('75" QLED TV', (1200, 2800))),
    ),
    "audio": CategoryProfile(
        "AUD",
        (1.3, 1.8),
        (15, 25),
        (168, 504),
        (-3.0, -1.5),
        (1, 8),
        (
            ("wireless headphones", (80, 400)),
            ("soundbar", (150, 600)),
            ("bluetooth speaker", (50, 250)),
        ),
    ),
    # Accessories: high margin, fast, cheap, short lead times. Elasticity stays below -1 so a
    # SKU with no competitor can't gain margin forever by raising its price.
    "accessories": CategoryProfile(
        "ACC",
        (2.0, 3.5),
        (30, 50),
        (72, 168),
        (-2.0, -1.1),
        (5, 25),
        (("HDMI cable", (5, 25)), ("TV wall mount", (25, 60)), ("USB-C charger", (10, 40))),
    ),
}
# Invented brands and suppliers; never use real retailer or brand names.
BRANDS = ("Kairo", "Veltro", "Nordwave", "Quillo", "Brevant")
SUPPLIERS = ("Pacific AV Distribution", "Harbour Electronics Trade", "Meridian Imports")


@dataclass(frozen=True)
class CatalogueItem:
    sku: str
    category: str
    cost_ex_gst_cents: int
    price_inc_gst_cents: int
    margin_floor_pct: int
    lead_time_hours: int
    elasticity: float
    base_daily_demand: float
    plausible_low_cents: int
    plausible_high_cents: int
    supplier_description: str  # the prompt-injection fault (M1.7) appends to this text
    promo_lock_until: datetime | None


def generate_catalogue(seed: int, start: datetime) -> list[CatalogueItem]:
    rng = rng_for(seed, "catalogue")
    items = []
    for category, p in PROFILES.items():
        for n in range(1, SKUS_PER_CATEGORY + 1):
            noun, cost_dollars = p.products[rng.integers(len(p.products))]
            cost = Cents(round(rng.uniform(*cost_dollars) * 100))
            ex_price = cost * rng.uniform(*p.markup)
            # shelf prices end in .95, like a real store
            price = int(round(inc_from_ex(Cents(round(ex_price))) / 100)) * 100 - 5
            brand = BRANDS[rng.integers(len(BRANDS))]
            supplier = SUPPLIERS[rng.integers(len(SUPPLIERS))]
            locked = rng.random() < PROMO_LOCK_SHARE
            items.append(
                CatalogueItem(
                    sku=f"{p.prefix}-{n:03d}",
                    category=category,
                    cost_ex_gst_cents=cost,
                    price_inc_gst_cents=price,
                    margin_floor_pct=int(rng.integers(p.floor_pct[0], p.floor_pct[1] + 1)),
                    lead_time_hours=int(rng.integers(p.lead_hours[0], p.lead_hours[1] + 1)),
                    elasticity=float(rng.uniform(*p.elasticity)),
                    base_daily_demand=float(rng.uniform(*p.daily_demand)),
                    plausible_low_cents=round(price * PLAUSIBLE_BAND[0]),
                    plausible_high_cents=round(price * PLAUSIBLE_BAND[1]),
                    supplier_description=f"{brand} {noun}, supplied by {supplier}.",
                    promo_lock_until=start + timedelta(days=int(rng.integers(2, 8)))
                    if locked
                    else None,
                )
            )
    return items
