"""Approval queue: decide pending proposals, re-checking P1-P9 before anything executes (S7)."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from pricing_watch.models import Action, Approval, Proposal
from pricing_watch.policy.config import PolicyConfig
from pricing_watch.policy.rules import DbStateView, IncidentContext, evaluate
from pricing_watch.sim.repricer import execute
from pricing_watch.tracing import Tracer


def pending(s: Session) -> list[Approval]:
    return list(
        s.scalars(
            select(Approval).where(Approval.status == "pending").order_by(Approval.proposal_id)
        )
    )


def decide(
    s: Session,
    tracer: Tracer,
    config: PolicyConfig,
    proposal_id: int,
    *,
    approve: bool,
    reviewer: str,
    note: str,
    t: datetime,
) -> Action | None:
    """Record the reviewer's call. An approval executes only if P1-P9 still pass at time t."""
    approval = s.get_one(Approval, proposal_id)
    if approval.status != "pending":
        raise ValueError(f"proposal {proposal_id} is already {approval.status}")
    approval.status = "approved" if approve else "rejected"
    approval.reviewer, approval.decided_at, approval.note = reviewer, t, note
    s.flush()
    if not approve:
        return None
    proposal = s.get_one(Proposal, proposal_id)
    # its own trace, because the incident trace may already be closed
    with tracer.observation(f"approval_execute:{proposal_id}", "agent", {"reviewer": reviewer}):
        with tracer.observation(f"recheck:{proposal_id}", "guardrail", proposal.args) as obs:
            # shortcut: no incident scope or suspect set yet; load them from the incident in M3
            recheck = evaluate(proposal.tool, proposal.args, DbStateView(s, t),
                               IncidentContext(), config, skip_approval=True)  # fmt: skip
            obs.set_output({"decision": recheck.decision, "rule_ids": list(recheck.rule_ids),
                            "detail": recheck.detail, "checked": recheck.checked})  # fmt: skip
        if recheck.decision != "allowed":
            approval.note = f"{note} | not executed: {', '.join(recheck.rule_ids)} {recheck.detail}"
            s.flush()
            return None
        return execute(s, tracer, proposal, t)


def scripted_review(
    s: Session, tracer: Tracer, config: PolicyConfig, t: datetime, *, approve: bool
) -> list[Action]:
    """Scenario reviewer: decide every pending approval in the same tick, the same way."""
    actions = [
        decide(s, tracer, config, a.proposal_id, approve=approve, reviewer="script", note="", t=t)
        for a in pending(s)
    ]
    return [a for a in actions if a is not None]
