"""Approval queue: decide pending proposals, re-checking P1-P9 before anything executes (S7)."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from pricing_watch.agent.triggers import incident_context
from pricing_watch.models import Action, Approval, Incident, Proposal
from pricing_watch.policy.config import PolicyConfig
from pricing_watch.policy.decide import guardrail_output
from pricing_watch.policy.rules import DbStateView, IncidentContext, evaluate
from pricing_watch.sim.repricer import execute
from pricing_watch.tracing import Tracer, contract


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
    with tracer.observation(*contract.approval_execute(proposal_id), {"reviewer": reviewer}):
        with tracer.observation(*contract.recheck(proposal_id), proposal.args) as obs:
            incident = s.get(Incident, proposal.incident_id) if proposal.incident_id else None
            ctx = incident_context(s, incident) if incident else IncidentContext()
            recheck = evaluate(proposal.tool, proposal.args, DbStateView(s, t), ctx, config,
                               skip_approval=True)  # fmt: skip
            obs.set_output(guardrail_output(recheck))
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
