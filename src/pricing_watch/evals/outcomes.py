"""Outcome scores: the agent against a rules-only baseline run with the same seed.

1.0 when the agent wins, 0.5 within the tie tolerance, 0.0 when it loses. Three levels read
straight onto the success criterion ("beats baseline in at least half, loses on none") and leave
no doubt where "close enough" ends. The tolerances were fixed in the spec before looking at any
results; change them there first. Inputs are run summaries (Summary, as dicts).
"""

from collections.abc import Sequence
from typing import Any

from pricing_watch.evals import Score, Skip

MARGIN_TIE_PCT = 1  # of the baseline's gross margin
STOCKOUT_TIE_PCT, STOCKOUT_TIE_HOURS = 5, 2  # of the baseline's SKU-hours, or this many if more
SELL_THROUGH_TIE = 0.01  # one percentage point


def _verdict(won: bool, tie: bool, comment: str) -> Score:
    return Score(0.5 if tie else 1.0 if won else 0.0, comment)


def margin_vs_baseline(agent: dict[str, Any], baseline: dict[str, Any]) -> Score:
    ours, theirs = agent["gross_margin_ex_gst_cents"], baseline["gross_margin_ex_gst_cents"]
    tie = abs(ours - theirs) * 100 <= abs(theirs) * MARGIN_TIE_PCT
    return _verdict(ours > theirs, tie, f"margin ${ours / 100:,.0f} vs ${theirs / 100:,.0f}")


def stockout_hours_vs_baseline(agent: dict[str, Any], baseline: dict[str, Any]) -> Score:
    ours, theirs = agent["stockout_sku_hours"], baseline["stockout_sku_hours"]
    tie = abs(ours - theirs) * 100 <= max(theirs * STOCKOUT_TIE_PCT, STOCKOUT_TIE_HOURS * 100)
    return _verdict(ours < theirs, tie, f"{ours} SKU-hours out of stock vs {theirs}")


def slow_mover_sell_through(
    agent: dict[str, Any], baseline: dict[str, Any], slow_movers: Sequence[str]
) -> Score | Skip:
    """Mean sell-through on the SKUs that opened a slow_mover incident in the agent's run."""
    if not slow_movers:
        return Skip("no slow_mover incident")
    ours = sum(agent["sell_through"][s] for s in slow_movers) / len(slow_movers)
    theirs = sum(baseline["sell_through"][s] for s in slow_movers) / len(slow_movers)
    tie = abs(ours - theirs) <= SELL_THROUGH_TIE
    return _verdict(ours > theirs, tie, f"sell-through {ours:.1%} vs {theirs:.1%} on "
                                        f"{len(slow_movers)} slow movers")  # fmt: skip


def beats_baseline(margin_scores: Sequence[float]) -> Score:
    """The success criterion over a run: margin won in at least half the scenarios, lost in none."""
    won, lost = margin_scores.count(1.0), margin_scores.count(0.0)
    passed = bool(margin_scores) and won * 2 >= len(margin_scores) and not lost
    return Score(1.0 if passed else 0.0,
                 f"won {won}, tied {len(margin_scores) - won - lost}, lost {lost}")  # fmt: skip
