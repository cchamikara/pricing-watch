"""Outcome scores: the agent against a rules-only baseline run with the same seed.

1.0 when the agent wins, 0.5 within the tie tolerance, 0.0 when it loses. Three levels read
straight onto the success criterion ("beats baseline in at least half, loses on none") and leave
no doubt where "close enough" ends. The tolerances were fixed in the spec before looking at any
results; change them there first. Inputs are run summaries (Summary, as dicts), except
escalation_loop, which reads the run's incidents.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pricing_watch.evals import Score, Skip
from pricing_watch.models import Action, Incident, Proposal

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


LOOP_OPENINGS, LOOP_WINDOW = 3, timedelta(days=7)


def escalation_loop(s: Session, start: datetime, end: datetime) -> Score | Skip:
    """0 when the same SKU and kind open three times in a row within 7 days with nothing executed
    in any of them; 1 otherwise. A notification is telling, not doing, so it doesn't count; a
    review request hands the problem to a person, so it does. The cool-down keeps an incident
    from reopening for 3 days, so a loop shows as one opening per cool-down."""
    incidents = list(s.scalars(
        select(Incident).where(Incident.opened_at >= start, Incident.opened_at < end,
                               Incident.sku.is_not(None))
        .order_by(Incident.opened_at, Incident.id)
    ))  # fmt: skip
    if not incidents:
        return Skip("no incidents")
    acted = set(s.scalars(
        select(Proposal.incident_id).join(Action, Action.proposal_id == Proposal.id)
        .where(Action.tool != "notify_merchandiser")
    ))  # fmt: skip
    by_key: dict[tuple[str, str | None], list[Incident]] = {}
    for i in incidents:
        by_key.setdefault((i.kind, i.sku), []).append(i)
    loops = []
    for (kind, sku), opened in by_key.items():
        for run in zip(*(opened[k:] for k in range(LOOP_OPENINGS)), strict=False):
            if (all(i.id not in acted for i in run)
                    and run[-1].opened_at - run[0].opened_at <= LOOP_WINDOW):  # fmt: skip
                loops.append(f"{kind} on {sku} from {run[0].opened_at:%Y-%m-%d}")
                break
    if loops:
        return Score(0.0, f"{len(loops)} loops: {'; '.join(loops[:5])}")
    return Score(1.0, f"no loop in {len(incidents)} incidents")
