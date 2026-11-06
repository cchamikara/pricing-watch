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
