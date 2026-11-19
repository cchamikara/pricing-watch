"""The message-quality judge: four pass/fail questions about an incident's closing notice.

The judge sees the notice and the system's record of what really happened (each proposal and
whether it was executed, is waiting for a person, or was refused), never the agent's own account,
so a notice that claims more than the record fails. The prompt is versioned in Langfuse as
`pricing-judge` [system, user]; the local templates are the fallback. A judge answer that doesn't
parse gives no scores: no score is better than a wrong one.

The rule judge answers the same four questions with simple checks: a free baseline for the
calibration, and what tests and the stand-in runs use.
"""

import html
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pricing_watch.agent.model import ModelError, ModelProvider
from pricing_watch.agent.prompts import render
from pricing_watch.evals import Score
from pricing_watch.evals.loader import TraceView

log = logging.getLogger(__name__)

DIMENSIONS = ("clarity", "severity_fit", "says_what_was_done", "says_what_needs_a_person")
JUDGE_NAME = "pricing-judge"  # the Langfuse chat prompt: [system, user]
JUDGE_TIMEOUT_S = 60
TEMPLATES = Path(__file__).with_name("templates")
STATES = {"allowed": "executed", "needs_approval": "waiting for a person's approval"}
ASKS = ("review", "approv", "check", "please", "expedite")  # words that ask a person to act
_SKU = re.compile(r"\b[A-Z]{2,5}-\d{3}\b")


@dataclass(frozen=True)
class JudgePrompts:
    system: str
    user: str
    version: int | None = None  # the Langfuse version; None for the local templates


LOCAL_JUDGE = JudgePrompts(system=(TEMPLATES / "judge_system.md").read_text(),
                           user=(TEMPLATES / "judge_user.md").read_text())  # fmt: skip


class _Verdict(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    passed: bool = Field(alias="pass")
    reason: str


class _Verdicts(BaseModel):
    clarity: _Verdict
    severity_fit: _Verdict
    says_what_was_done: _Verdict
    says_what_needs_a_person: _Verdict


def facts(view: TraceView) -> dict[str, str] | None:
    """What the judge is shown, or None when there is nothing to judge."""
    notice, incident = view.notification, view.incident
    if incident is None or notice is None or notice.message == "[masked]":
        return None
    who = "the model" if incident.answer_source == "model" else "the rules fallback"
    lines = []
    for gate in view.guardrails:
        args = gate.proposal.get("args", {})
        target = " ".join(args["skus"]) if "skus" in args else args.get("sku", "")
        state = STATES.get(gate.output.decision,
                           f"refused by policy ({', '.join(gate.output.rule_ids)})")  # fmt: skip
        lines.append(f"- {gate.proposal['tool']} {target}: {state}")
    return {"incident": f"{incident.incident_kind} on {incident.sku or incident.category} "
                        f"(answered by {who})",
            "outcome": "\n".join(lines) or "- nothing was proposed",
            "severity": notice.severity, "message": notice.message}  # fmt: skip


def judge(view: TraceView, model: ModelProvider, prompts: JudgePrompts) -> dict[str, Score] | None:
    known = facts(view)
    if known is None:
        return None
    escaped = {k: html.escape(v) for k, v in known.items()}  # the notice can't close its block
    messages = [{"role": "system", "content": prompts.system},
                {"role": "user", "content": render(prompts.user, escaped)}]  # fmt: skip
    try:
        text = model.complete(messages, JUDGE_TIMEOUT_S).raw_text.strip()  # type: ignore[arg-type]
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```")
        verdicts = _Verdicts.model_validate(json.loads(text))
    except (ModelError, ValueError, ValidationError) as e:  # JSONDecodeError is a ValueError
        log.warning("judge gave no usable answer for %s: %s", view.trace_id, e)
        return None
    return {d: Score(1.0 if (v := getattr(verdicts, d)).passed else 0.0, v.reason)
            for d in DIMENSIONS}  # fmt: skip


def rule_judge(view: TraceView) -> dict[str, Score]:
    """The four questions by simple checks on the same record."""
    known, incident = facts(view), view.incident
    if known is None or incident is None:
        return {}
    text, severity = known["message"].lower(), known["severity"]
    decisions = [g.output.decision for g in view.guardrails]
    review = any(g.proposal.get("tool") == "request_review" for g in view.guardrails)
    needs_person = "needs_approval" in decisions or review
    # a category-wide incident (a listing match, margin erosion) may name the product it's about
    if incident.sku:
        named = incident.sku.lower() in text
    else:
        named = incident.category.lower() in text or bool(_SKU.search(known["message"]))
    if decisions:
        said = "executed" in text or "policy:" in text
    else:
        said = "no action" in text or "holding" in text
    asked = not needs_person or any(word in text for word in ASKS)
    checks = {
        "clarity": (named and len(text) <= 600 and not text.startswith("{"),
                    "names the product, short, plain"),
        "severity_fit": ((severity == "action_needed") == needs_person,
                         f"{severity}; a person {'is' if needs_person else 'is not'} needed"),
        "says_what_was_done": (said, "says what happened" if said else "doesn't say"),
        "says_what_needs_a_person": (asked, "asks for what's needed" if asked else "doesn't ask"),
    }  # fmt: skip
    return {d: Score(1.0 if ok else 0.0, why) for d, (ok, why) in checks.items()}


@dataclass(frozen=True)
class Judge:
    """What the worker runs, and its name: rules, or the model and prompt version (claude:v1,
    gemini:local). The name keys its scores, so two judges never overwrite each other."""

    run: Callable[[TraceView], dict[str, Score] | None]
    name: str


def fetch_judge(client: Any, label: str) -> JudgePrompts:
    try:
        prompt = client.get_prompt(JUDGE_NAME, label=label, type="chat", max_retries=0,
                                   fetch_timeout_seconds=5)  # fmt: skip
        system, user = (str(m["content"]) for m in prompt.prompt)
        render(user, dict.fromkeys(("incident", "outcome", "severity", "message"), ""))
        return JudgePrompts(system=system, user=user, version=int(prompt.version))
    except Exception as e:  # unreachable, missing, or placeholders the code can't fill
        log.warning("judge prompt %s@%s unusable (%s); using the local templates",
                    JUDGE_NAME, label, e)  # fmt: skip
        return LOCAL_JUDGE


def make_judge(model: str | None, client: Any) -> Judge | None:
    """The worker's judge from JUDGE_MODEL: unset, none; stand-in, the rule judge; claude or
    gemini, that model with the judge prompt labelled production."""
    if not model:
        return None
    if model == "stand-in":
        return Judge(run=rule_judge, name="rules")
    from pricing_watch.agent.model import ClaudeProvider, GeminiProvider

    provider = ClaudeProvider() if model == "claude" else GeminiProvider()
    prompts = fetch_judge(client, "production")
    name = f"{model}:{f'v{prompts.version}' if prompts.version else 'local'}"
    return Judge(run=lambda view: judge(view, provider, prompts), name=name)
