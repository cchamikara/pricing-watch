"""Read a trace from Langfuse into contract models, failing loudly on shape drift.

Every observation's name and type must be in the trace contract, and every output an evaluator
reads must parse into its contract model. Anything else raises ContractError: an evaluator that
quietly skipped a trace it couldn't read would report a clean score for an unread trace.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from langfuse import Langfuse
from pydantic import BaseModel, ValidationError

from pricing_watch.tracing import contract


class ContractError(Exception):
    """A trace doesn't match the trace contract: fix the emitter or the contract, don't skip."""


@dataclass(frozen=True)
class Seen:
    id: str
    name: str
    type: str  # the contract's lower-case type
    parent_id: str | None
    start_time: datetime
    input: Any
    output: Any
    metadata: dict[str, Any]


@dataclass(frozen=True)
class Gate:
    proposal: dict[str, Any]  # source, tool, args, reason
    output: contract.GuardrailOutput


@dataclass(frozen=True)
class Execution:
    tool: str
    args: dict[str, Any]
    snapshot: BaseModel  # the tool's model from contract.EXECUTE_SNAPSHOTS


@dataclass(frozen=True)
class Notice:
    severity: str
    message: str


@dataclass(frozen=True)
class TraceView:
    trace_id: str
    root: Seen
    trace: contract.TraceAttributes
    incident: contract.IncidentAttributes | None  # set for incident roots
    observations: list[Seen]  # everything under the root, in start order
    reads: list[str]  # read tool names, in order
    guardrails: list[Gate]
    executions: list[Execution]
    notification: Notice | None


def fetch_trace(client: Langfuse, trace_id: str) -> TraceView:
    found = client.api.observations.get_many(trace_id=trace_id, limit=1000,
                                             fields="core,basic,io,metadata")  # fmt: skip
    return load_trace(found.data)


def load_trace(observations: Sequence[Any]) -> TraceView:
    if not observations:
        raise ContractError("no observations")
    trace_id = str(observations[0].trace_id)
    seen = sorted((_seen(trace_id, o) for o in observations), key=lambda o: o.start_time)
    # the root's parent isn't in the trace: a seeded trace id gives it a synthetic parent id
    ids = {o.id for o in seen}
    roots = [o for o in seen if o.parent_id not in ids]
    if len(roots) != 1:
        raise ContractError(f"trace {trace_id}: {len(roots)} root observations, expected 1")
    root, rest = roots[0], [o for o in seen if o is not roots[0]]
    incident = None
    if root.name == contract.INCIDENT.name:
        incident = _parse(trace_id, root.name, contract.IncidentAttributes, root.metadata)
    notices = [o for o in rest if o.name == contract.NOTIFY.name]
    return TraceView(
        trace_id=trace_id, root=root, incident=incident, observations=rest,
        trace=_parse(trace_id, root.name, contract.TraceAttributes, root.metadata),
        reads=[o.name for o in rest if o.name.startswith("get_")],
        guardrails=[Gate(proposal=_dict(trace_id, o, o.input),
                         output=_parse(trace_id, o.name, contract.GuardrailOutput, o.output))
                    for o in rest if o.name.startswith("policy:")],
        executions=[_execution(trace_id, o) for o in rest if o.name.startswith("execute:")],
        notification=_parse(trace_id, notices[-1].name, Notice, notices[-1].input) if notices
        else None,
    )  # fmt: skip


def _seen(trace_id: str, o: Any) -> Seen:
    expected = contract.observation_for(o.name)
    if expected is None:
        raise ContractError(f"trace {trace_id}: {o.name!r} is not in the trace contract")
    if str(o.type).lower() != expected.type:
        raise ContractError(f"trace {trace_id}: {o.name!r} is {str(o.type).lower()}, "
                            f"the contract says {expected.type}")  # fmt: skip
    return Seen(id=o.id, name=o.name, type=expected.type, parent_id=o.parent_observation_id,
                start_time=o.start_time, input=_json(o.input), output=_json(o.output),
                metadata=dict(o.metadata or {}))  # fmt: skip


def _json(value: Any) -> Any:
    """The API returns input and output as JSON text; plain text stays text."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _dict(trace_id: str, o: Seen, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"trace {trace_id}: {o.name!r} input is not an object")
    return value


def _parse[M](trace_id: str, where: str, model: type[M], value: Any) -> M:
    try:
        if isinstance(model, type) and issubclass(model, BaseModel):
            return model.model_validate(value)
        return model(**value)
    except (ValidationError, TypeError) as e:
        raise ContractError(f"trace {trace_id}: {where} doesn't fit {model.__name__}: "
                            f"{str(e)[:300]}") from e  # fmt: skip


def _execution(trace_id: str, o: Seen) -> Execution:
    tool = o.name.split(":", 1)[1]
    model = contract.EXECUTE_SNAPSHOTS.get(tool)
    if model is None:
        raise ContractError(f"trace {trace_id}: no snapshot shape for tool {tool!r}")
    return Execution(tool=tool, args=_dict(trace_id, o, o.input),
                     snapshot=_parse(trace_id, o.name, model, o.output))  # fmt: skip
