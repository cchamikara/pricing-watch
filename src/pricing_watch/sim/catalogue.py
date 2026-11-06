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
    cost_dollars: tuple[float, float]
    markup: tuple[float, float]  # shelf price ex GST / cost
    floor_pct: tuple[int, int]
    lead_hours: tuple[int, int]
    elasticity: tuple[float, float]
    daily_demand: tuple[float, float]
    nouns: tuple[str, ...]


PROFILES = {
    # TVs: thin margins, price-sensitive, slow, imported. Keep min markup above max floor.
    "tvs": CategoryProfile(
        "TV",
        (400, 3000),
        (1.15, 1.30),
        (5, 10),
        (336, 672),
        (-3.5, -2.0),
        (0.3, 3),
        ('55" 4K TV', '65" OLED TV', '75" QLED TV'),
    ),
    "audio": CategoryProfile(
        "AUD",
        (50, 600),
        (1.3, 1.8),
        (15, 25),
        (168, 504),
        (-3.0, -1.5),
        (1, 8),
        ("wireless headphones", "soundbar", "bluetooth speaker"),
    ),
    # Accessories: high margin, fast, cheap, short lead times.
    "accessories": CategoryProfile(
        "ACC",
        (5, 60),
        (2.0, 3.5),
        (30, 50),
        (72, 168),
        (-1.5, -0.5),
        (5, 25),
        ("HDMI cable", "TV wall mount", "USB-C charger"),
    ),
}
# Invented brands and suppliers; never use real retailer or brand names.
BRANDS = ("Kairo", "Veltro", "Nordwave", "Sonik", "Aurex")
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
            cost = Cents(round(rng.uniform(*p.cost_dollars) * 100))
            ex_price = cost * rng.uniform(*p.markup)
            # shelf prices end in .95, like a real store
            price = int(round(inc_from_ex(Cents(round(ex_price))) / 100)) * 100 - 5
            brand = BRANDS[rng.integers(len(BRANDS))]
            noun = p.nouns[rng.integers(len(p.nouns))]
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
