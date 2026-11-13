"""Incident loop: reads, diagnose, policy per proposal, execute or queue, then notify and close.

The notification is sent in a `finally`, so every incident ends with exactly one, even when a step
raises. A model failure (timeout, error, unparsable output) falls back to the rules plan
(`agent/fallback.py`), which goes through policy the same way.
"""

import logging
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy.orm import Session

from pricing_watch.agent.fallback import rules_plan
from pricing_watch.agent.model import Brief, Message, ModelError, ModelProvider, ModelResponse
from pricing_watch.agent.output import ModelOutput, parse_output
from pricing_watch.agent.prompts import LOCAL_PROMPTS, Prompts, incident_prompt
from pricing_watch.agent.tools import CompetitorRead, ProductRead, ReadTools, StockRead
from pricing_watch.agent.triggers import incident_context
from pricing_watch.models import Incident, Notification
from pricing_watch.policy.config import PolicyConfig
from pricing_watch.policy.decide import propose
from pricing_watch.policy.rules import DbStateView
from pricing_watch.policy.schemas import Severity
from pricing_watch.sim.faults import Fault
from pricing_watch.sim.repricer import execute
from pricing_watch.tracing import Tracer, contract

MODEL_TIMEOUT_S = 60.0
log = logging.getLogger("pricing_watch")


@dataclass
class _Outcome:
    severity: Severity
    text: str
    answer_source: str = "model"
    model_reachable: bool = False
    decisions: Counter[str] = field(default_factory=Counter)
    marks: list[str] = field(default_factory=list)  # "tool ✓|⏸|✗ rule" per proposal, for the log
    executed: int = 0
    model_seconds: float = 0.0


def handle_incident(
    s: Session,
    tracer: Tracer,
    model: ModelProvider,
    config: PolicyConfig,
    incident: Incident,
    t: datetime,
    faults: Sequence[Fault] = (),
    run: contract.RunTrace | None = None,  # which traced run: seeds the trace id
    prompts: Prompts = LOCAL_PROMPTS,  # from Langfuse by label, or the local templates
) -> Notification:
    scope = incident.sku or incident.category
    session = contract.incident_session(incident.category, incident.sku, incident.kind,
                                        t.date().isoformat())  # fmt: skip
    trace = contract.root_trace(run, "incident", incident.id, t, str(config.policy_version),
                                session_id=session, category=incident.category,
                                sku=incident.sku, incident_kind=incident.kind)  # fmt: skip
    if trace:
        incident.trace_id = contract.trace_id(trace.trace_seed)
        incident.session_id = session
    trigger = {"kind": incident.kind, "category": incident.category, "sku": incident.sku,
               "sim_time": t.isoformat()}  # fmt: skip
    out = _Outcome("action_needed", f"{incident.kind} on {scope}: the agent stopped; please check.")
    with tracer.observation(*contract.INCIDENT, trigger, trace=trace) as root:
        try:
            _work(s, tracer, model, config, incident, t, faults, out, prompts)
        except Exception as e:
            out.severity = "action_needed"
            error = f"{type(e).__name__}: {e}"
            out.text = f"{incident.kind} on {scope}: the agent failed ({error}); please check."
            out.text = out.text[:2000]  # the notify_merchandiser schema's cap
            raise
        finally:
            # shortcut: assumes the session can still write; a failed flush loses the notice
            notice = {"severity": out.severity, "message": out.text}
            with tracer.observation(*contract.NOTIFY, notice) as obs:
                note = Notification(incident_id=incident.id, severity=out.severity,
                                    message=out.text, sent_at=t)  # fmt: skip
                s.add(note)
                incident.closed_at, incident.answer_source = t, out.answer_source
                s.flush()
                obs.set_output(contract.NotifyOutput(notification_id=note.id).model_dump())
            root.set_attributes(**contract.IncidentAttributes(
                incident_kind=incident.kind, category=incident.category, sku=incident.sku,
                answer_source=out.answer_source, model_reachable=out.model_reachable,
                proposals_total=out.decisions.total(),
                proposals_refused=out.decisions["refused"],
                proposals_pending=out.decisions["needs_approval"],
                actions_executed=out.executed,
            ).model_dump())  # fmt: skip
            root.set_output(
                contract.IncidentOutput(severity=out.severity, message=out.text).model_dump()
            )
            first_line = out.text.splitlines()[0][:80] if out.text else ""
            log.info("%s  %-19s %-8s %-14s %5.1fs  %-13s %-36s %s", t.strftime("%Y-%m-%d %H:%M"),
                     incident.kind, scope, out.answer_source, out.model_seconds, out.severity,
                     ", ".join(out.marks) or "-", first_line)  # fmt: skip
    return note


def _work(
    s: Session,
    tracer: Tracer,
    model: ModelProvider,
    config: PolicyConfig,
    incident: Incident,
    t: datetime,
    faults: Sequence[Fault],
    out: _Outcome,
    prompts: Prompts,
) -> None:
    reads = ReadTools(s, tracer, t, config, faults)
    found: list[BaseModel] = []
    product: ProductRead | None = None
    stock: StockRead | None = None
    competitors: CompetitorRead | None = None
    forecast = 0.0
    if incident.sku:
        product, stock = reads.get_product(incident.sku), reads.get_stock(incident.sku)
        competitors = reads.get_competitor_prices(incident.sku)
        fc = reads.get_demand_forecast(incident.sku)
        found += [product, stock, competitors, fc]
        forecast = fc.daily_units
    # shortcut: a category incident reads only its notices and the policy, no per-SKU reads
    notices = reads.get_supplier_notices(incident.category, incident.sku)
    found.append(notices)
    listing = reads.get_listing(incident.listing_id) if incident.listing_id else None
    if listing:
        found.append(listing)
    policy = reads.get_policy()
    found.append(policy)
    quotes = competitors.prices if competitors else []
    plausible = [q.price_inc_gst_cents for q in quotes if q.plausible]
    brief = Brief(kind=incident.kind, category=incident.category, sku=incident.sku, now=t,
                  product=product, stock=stock, forecast_daily=forecast,
                  suspect=incident.sku in incident.suspect_skus,
                  notes=tuple(q.note for q in competitors.prices if q.note) if competitors else (),
                  notices=tuple(n.text for n in notices.notices),
                  cheapest_plausible=min(plausible, default=None),
                  listing_id=incident.listing_id,
                  listing_title=listing.title if listing else "",
                  candidates=tuple((c.sku, c.title) for c in listing.candidates)
                  if listing else ())  # fmt: skip
    view, ctx = DbStateView(s, t), incident_context(s, incident)
    reply, source = _diagnose(tracer, model, incident, t, found, brief, out, prompts), "model"
    if reply is None:  # the model timed out, failed or broke the schema
        reply, source = rules_plan(brief, competitors, policy, view), "rules"
        out.answer_source = "rules_fallback"
    out.severity, out.text = reply.message.severity, reply.message.text

    plan = reply.proposals
    for p in plan:
        proposal, decision = propose(
            s, tracer, view, ctx, config, source=source, tool=p.tool, args=p.args,
            reason=p.reason, incident_id=incident.id,
        )  # fmt: skip
        out.decisions[decision.decision] += 1
        mark = {"allowed": "✓", "needs_approval": "⏸"}.get(decision.decision, "✗")
        rule = f" {decision.rule_ids[0].split('-')[0]}" if decision.decision == "refused" else ""
        shown = " ".join(p.tool.split())[:40]  # model text: no newlines, so it can't forge a line
        out.marks.append(f"{shown} {mark}{rule}")
        if decision.decision == "allowed":
            execute(s, tracer, proposal, t)
            out.executed += 1
    if plan:  # the model's text was written before policy ran; say what really happened
        pending = out.decisions["needs_approval"]
        tally = (f"Policy: {out.executed} executed, {pending} waiting for approval, "
                 f"{out.decisions['refused']} refused.")  # fmt: skip
        out.text = f"{out.text[: 1999 - len(tally)]}\n{tally}"  # the tally survives the 2000 cap
        if pending:
            out.severity = "action_needed"


def _diagnose(
    tracer: Tracer,
    model: ModelProvider,
    incident: Incident,
    t: datetime,
    found: list[BaseModel],
    brief: Brief,
    out: _Outcome,
    prompts: Prompts,
) -> ModelOutput | None:
    """Ask the model; None means it timed out, failed or answered outside the schema."""
    scope = incident.sku or incident.category
    messages: list[Message] = [
        {"role": "system", "content": prompts.system},
        {"role": "user",
         "content": incident_prompt(incident.kind, scope, t, incident.suspect_skus, found,
                                    prompts.incident)},
    ]  # fmt: skip
    with tracer.observation(*contract.DIAGNOSE, messages) as gen:
        started = time.perf_counter()  # wall time for display only; the simulation never reads it
        response: ModelResponse | None = None
        try:
            response = model.complete(messages, MODEL_TIMEOUT_S, brief)
            out.model_reachable = True
            gen.set_output(response.raw_text)
            return parse_output(response.raw_text)
        except ModelError as e:
            gen.set_output(contract.GenerationError(error=f"{type(e).__name__}: {e}").model_dump())
            return None
        finally:
            out.model_seconds = round(time.perf_counter() - started, 1)
            gen.set_attributes(**contract.GenerationAttributes(
                latency_s=out.model_seconds,
                model=response.model_name if response else None,
                usage=response.usage if response else None,
                prompt_name=prompts.name, prompt_version=prompts.version,
            ).model_dump())  # fmt: skip
