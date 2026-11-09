"""Record every proposal and its policy decision, in SQLite and as a guardrail observation."""

from typing import Any

from sqlalchemy.orm import Session

from pricing_watch.models import PolicyDecision, Proposal
from pricing_watch.policy.config import PolicyConfig
from pricing_watch.policy.rules import Decision, IncidentContext, StateView, evaluate
from pricing_watch.tracing import Tracer


def propose(
    s: Session,
    tracer: Tracer,
    view: StateView,
    ctx: IncidentContext,
    config: PolicyConfig,
    *,
    source: str,
    tool: str,
    args: dict[str, Any],
    reason: str,
    incident_id: int | None = None,
) -> tuple[Proposal, Decision]:
    """Save the proposal as given (even if invalid), decide it, and save the decision."""
    proposal = Proposal(incident_id=incident_id, source=source, tool=tool, args=args, reason=reason)
    s.add(proposal)
    s.flush()
    given = {"source": source, "tool": tool, "args": args, "reason": reason}
    with tracer.observation(f"policy:{proposal.id}", "guardrail", given) as obs:
        decision = evaluate(tool, args, view, ctx, config)
        s.add(
            PolicyDecision(
                proposal_id=proposal.id,
                decision=decision.decision,
                rule_ids=list(decision.rule_ids),
                detail=decision.detail,
                policy_version=decision.policy_version,
            )
        )
        s.flush()
        obs.set_output(
            {
                "decision": decision.decision,
                "rule_ids": list(decision.rule_ids),
                "detail": decision.detail,
                "checked": decision.checked,
            }
        )
    return proposal, decision
