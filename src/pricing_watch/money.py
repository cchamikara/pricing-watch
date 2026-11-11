"""Money as integer cents, and GST conversion.

Rounding rule: every GST conversion rounds to the nearest cent, with halves rounded up.
Inputs must be non-negative; amounts are prices, never deltas.
"""

from typing import NewType

Cents = NewType("Cents", int)

GST_RATE_PERCENT = 10


def _div_round_half_up(numerator: int, denominator: int) -> int:
    return (2 * numerator + denominator) // (2 * denominator)


def _check(amount: Cents) -> None:
    if amount < 0:
        raise ValueError(f"amount must be non-negative, got {amount}")


def ex_from_inc(inc_gst: Cents) -> Cents:
    """Ex-GST price from a GST-inclusive price."""
    _check(inc_gst)
    return Cents(_div_round_half_up(inc_gst * 100, 100 + GST_RATE_PERCENT))


def inc_from_ex(ex_gst: Cents) -> Cents:
    """GST-inclusive price from an ex-GST price."""
    _check(ex_gst)
    return Cents(_div_round_half_up(ex_gst * (100 + GST_RATE_PERCENT), 100))


def ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def floor_price(cost_ex_gst_cents: int, margin_floor_pct: int) -> int:
    """Lowest shelf price (inc GST) whose ex-GST price meets cost x (1 + floor).

    Solves ex_from_inc(p) >= x exactly under half-up rounding: p >= 1.1x - 0.55.
    """
    min_ex = ceil_div(cost_ex_gst_cents * (100 + margin_floor_pct), 100)
    return ceil_div(22 * min_ex - 11, 20)


def discounted(price_inc_gst_cents: int, discount_pct: int) -> int:
    """Promotion price: the discount rounds in our favour (down to the cent)."""
    return price_inc_gst_cents * (100 - discount_pct) // 100
