"""Demand model: lambda = base * (p / p_ref)^e * s(t) * h(t) * c(t), drawn hourly from a Poisson.

- base: the SKU's average daily demand at its starting price.
- (p / p_ref)^e: own-price effect; p_ref is the starting shelf price, e < 0.
- s(t): season (Black Friday and Christmas peak, EOFY in June) and a weekend lift.
- h(t): share of the day's demand in this hour; online shopping peaks in the evening.
- c(t): competitor effect from our price / the cheapest plausible competitor price.
"""

from datetime import datetime

import numpy as np

from pricing_watch.sim.catalogue import CatalogueItem

MONTH_FACTOR = {1: 0.8, 6: 1.3, 11: 1.4, 12: 1.6}  # other months 1.0
WEEKEND_FACTOR = 1.15
_HOURS = [1, 0.5, 0.3, 0.2, 0.2, 0.3, 0.6, 1, 1.4, 1.6, 1.7, 1.8,
          1.9, 1.8, 1.6, 1.6, 1.7, 1.9, 2.2, 2.6, 2.8, 2.5, 2, 1.5]  # fmt: skip
HOUR_SHARE = [w / sum(_HOURS) for w in _HOURS]
COMPETITOR_ELASTICITY = -3.0  # 10% dearer than the cheapest competitor loses about a quarter


def hourly_rate(
    item: CatalogueItem, price_inc_gst_cents: int, t: datetime, cheapest_competitor: int | None
) -> float:
    """Expected units demanded in the hour starting at t. No competitor price means no effect."""
    own = (price_inc_gst_cents / item.price_inc_gst_cents) ** item.elasticity
    season = MONTH_FACTOR.get(t.month, 1.0) * (WEEKEND_FACTOR if t.weekday() >= 5 else 1.0)
    competitor = (
        (price_inc_gst_cents / cheapest_competitor) ** COMPETITOR_ELASTICITY
        if cheapest_competitor
        else 1.0
    )
    return float(item.base_daily_demand * own * season * HOUR_SHARE[t.hour] * competitor)


def draw_sales(rng: np.random.Generator, rate: float, on_hand: int) -> tuple[int, int]:
    """Draw demand; sell what stock allows. Returns (units sold, units lost to stockout)."""
    demand = int(rng.poisson(rate))
    sold = min(demand, on_hand)
    return sold, demand - sold
