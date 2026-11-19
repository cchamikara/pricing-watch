"""Record every proposal and its policy decision, in SQLite and as a guardrail observation."""

from typing import Any

from sqlalchemy.orm import Session

from pricing_watch.models import Approval, PolicyDecision, Proposal
from pricing_watch.policy.config import PolicyConfig
from pricing_watch.policy.rules import Decision, IncidentContext, StateView, evaluate
from pricing_watch.tracing import Tracer, contract


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
    with tracer.observation(*contract.policy(proposal.id), given) as obs:
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
        if decision.decision == "needs_approval":
            s.add(Approval(proposal_id=proposal.id, status="pending", reviewer="",
                           decided_at=None, note=""))  # fmt: skip
        s.flush()
        obs.set_output(guardrail_output(decision))
    return proposal, decision


def guardrail_output(decision: Decision) -> dict[str, Any]:
    """A decision as the trace contract's guardrail output, with the values the rules checked."""
    return contract.GuardrailOutput(
        decision=decision.decision, rule_ids=list(decision.rule_ids), detail=decision.detail,
        checked=decision.checked,
    ).model_dump(mode="json")  # fmt: skip
