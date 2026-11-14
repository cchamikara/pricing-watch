"""The trace contract: every observation name and type, attribute key and output shape that the
emitter writes and the evaluators read. Nothing else spells them out (a grep test checks), so a
rename here reaches both sides at once.

Each observation is an `Obs(name, type)`; emitters call `tracer.observation(*contract.X, input)`,
so a name can't be paired with the wrong type. Attributes and outputs are Pydantic models;
emitters pass `Model(...).model_dump(mode="json")`, so a misspelt key fails type checking.
"""

from datetime import datetime
from hashlib import sha256
from importlib.metadata import version
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel

from pricing_watch.tracing import ObservationType


class Obs(NamedTuple):
    name: str
    type: ObservationType


# roots
RUN = Obs("run", "span")  # each traced run's anchor: its trace id is known in advance
INCIDENT = Obs("incident", "agent")
ROUTINE_REPRICE = Obs("routine_reprice", "agent")

# inside an incident
DIAGNOSE = Obs("diagnose", "generation")
NOTIFY = Obs("notify_merchandiser", "tool")
GET_PRODUCT = Obs("get_product", "tool")
GET_STOCK = Obs("get_stock", "tool")
GET_COMPETITOR_PRICES = Obs("get_competitor_prices", "tool")
GET_DEMAND_FORECAST = Obs("get_demand_forecast", "tool")
GET_SUPPLIER_NOTICES = Obs("get_supplier_notices", "tool")
GET_LISTING = Obs("get_listing", "tool")
GET_POLICY = Obs("get_policy", "tool")


def policy(proposal_id: int) -> Obs:
    return Obs(f"policy:{proposal_id}", "guardrail")


def execute(tool: str) -> Obs:
    return Obs(f"execute:{tool}", "tool")


def approval_execute(proposal_id: int) -> Obs:
    """Its own root: the incident's trace may already be closed when a person approves."""
    return Obs(f"approval_execute:{proposal_id}", "agent")


def recheck(proposal_id: int) -> Obs:
    return Obs(f"recheck:{proposal_id}", "guardrail")


# traces: identity, session and the attributes known when a trace starts


def trace_id(seed: str) -> str:
    """The SDK's seeded trace id (Langfuse.create_trace_id), without importing the SDK."""
    return sha256(seed.encode()).digest()[:16].hex()


def incident_session(category: str, sku: str | None, kind: str, sim_day: str) -> str:
    return f"{category}:{sku or '-'}:{kind}:{sim_day}"


AGENT_VERSION = version("pricing-watch")


class TraceAttributes(BaseModel):
    """Set when a trace starts and propagated to every observation in it, so Langfuse can
    filter by them. Outcomes (answer_source, proposal counts) are only known at the end, so
    they are the root observation's IncidentAttributes instead."""

    scenario_id: str
    run_name: str
    sim_time: str
    agent_version: str
    policy_version: str
    category: str | None = None
    sku: str | None = None
    incident_kind: str | None = None


class TraceInfo(BaseModel):
    """What a root observation needs to start its trace."""

    trace_seed: str  # "<environment>:<run>:<scenario>:<seed>:<root>:<number>"
    session_id: str | None = None
    tags: list[str]  # the run name: one click in Langfuse filters a whole run
    trace_name: str  # "<scenario> · <root>", so the trace list shows the scenario
    attributes: TraceAttributes


class RunTrace(BaseModel):
    """Which traced run this is. Its parts seed every trace id: a re-run of the same run name
    in the same environment repeats them, and any other run never lands in its traces."""

    environment: str = "local"  # the Langfuse environment, so sim and ci never share traces
    run_name: str = "dev"  # e.g. an experiment's name; `sim run --trace` refuses a name twice
    scenario_id: str = ""
    seed: int = 0

    def anchor(self) -> str:
        """The trace id of the run's opening `run` trace, known before the run starts."""
        return trace_id(self._seed("run", 0))

    def _seed(self, root: str, number: object) -> str:
        return f"{self.environment}:{self.run_name}:{self.scenario_id}:{self.seed}:{root}:{number}"


def root_trace(
    run: RunTrace | None,
    root: str,
    number: object,
    t: datetime,
    policy_version: str,
    *,
    session_id: str | None = None,
    category: str | None = None,
    sku: str | None = None,
    incident_kind: str | None = None,
) -> TraceInfo | None:
    """A root's trace, seeded by its run, so a re-run of the same run name gives the same ids.
    No run (say, a CLI approval on an old run's database): None, so a random trace id."""
    if run is None:
        return None
    return TraceInfo(
        trace_seed=run._seed(root, number), session_id=session_id, tags=[run.run_name],
        trace_name=f"{run.scenario_id} · {root}",
        attributes=TraceAttributes(
            scenario_id=run.scenario_id, run_name=run.run_name,
            sim_time=t.isoformat(timespec="minutes"), agent_version=AGENT_VERSION,
            policy_version=policy_version, category=category, sku=sku,
            incident_kind=incident_kind,
        ),
    )  # fmt: skip


# attributes


class IncidentAttributes(BaseModel):
    incident_kind: str
    category: str
    sku: str | None = None  # absent for category-wide incidents: Langfuse drops None values
    suspect_skus: list[str] = []  # SKUs whose data looked wrong when the incident opened
    answer_source: str  # "model" or "rules_fallback"
    model_reachable: bool
    proposals_total: int
    proposals_refused: int
    proposals_pending: int
    actions_executed: int


class GenerationAttributes(BaseModel):
    latency_s: float  # wall time of the model call, success or not
    model: str | None = None  # the model that answered; None if it never did
    usage: dict[str, int] | None = None  # input_tokens, output_tokens
    prompt_name: str | None = None  # the Langfuse prompt it used; None: the local templates
    prompt_version: int | None = None


# outputs


class IncidentOutput(BaseModel):
    severity: str
    message: str


class GenerationError(BaseModel):
    error: str  # the call failed or broke the schema; the rules fallback answered


class RepriceOutput(BaseModel):
    actions_executed: int


DecisionKind = Literal["allowed", "needs_approval", "refused"]  # the policy's three answers


class GuardrailOutput(BaseModel):
    decision: DecisionKind
    rule_ids: list[str]
    detail: str
    checked: dict[str, Any]  # the values the rules compared against


class NotifyOutput(BaseModel):
    notification_id: int


# execute:<tool> outputs: the executor's authoritative snapshot, never the model's numbers


class SpendDelta(BaseModel):
    before: int
    after: int
    budget: int  # the day's markdown budget for the category


class PriceChangeSnapshot(BaseModel):
    cost_ex_gst_cents: int
    margin_floor_pct: int
    floor_price_inc_gst_cents: int
    running_discount_pct: int  # a promotion in force applies on top of the new price
    old_price_inc_gst_cents: int
    new_price_inc_gst_cents: int
    markdown_spent_cents: dict[str, SpendDelta]  # by category


class PromotedSku(BaseModel):
    cost_ex_gst_cents: int
    margin_floor_pct: int
    floor_price_inc_gst_cents: int
    price_inc_gst_cents: int
    promo_price_inc_gst_cents: int


class PromotionSnapshot(BaseModel):
    skus: dict[str, PromotedSku]
    markdown_spent_cents: dict[str, SpendDelta]


class ReorderSnapshot(BaseModel):
    on_hand: int
    inbound_before: int
    qty: int
    eta: str
    forecast_daily: float


class ReviewSnapshot(BaseModel):
    price_inc_gst_cents: int
    on_hand: int


class MatchSnapshot(BaseModel):
    title: str
    matched_sku: str | None
    was: str | None


ExecuteSnapshot = (
    PriceChangeSnapshot
    | PromotionSnapshot
    | ReorderSnapshot
    | ReviewSnapshot
    | MatchSnapshot
    | NotifyOutput
)


# reading traces back: what each observation name must be, and each tool's snapshot shape

_FIXED = {o.name: o for o in (RUN, INCIDENT, ROUTINE_REPRICE, DIAGNOSE, NOTIFY, GET_PRODUCT,
                              GET_STOCK, GET_COMPETITOR_PRICES, GET_DEMAND_FORECAST,
                              GET_SUPPLIER_NOTICES, GET_LISTING, GET_POLICY)}  # fmt: skip
_NUMBERED = {"policy": policy(0).type, "execute": execute("").type,
             "recheck": recheck(0).type, "approval_execute": approval_execute(0).type}  # fmt: skip


def observation_for(name: str) -> Obs | None:
    """The contract's observation for a name seen in a trace, or None if it isn't one."""
    if name in _FIXED:
        return _FIXED[name]
    prefix, _, rest = name.partition(":")
    kind = _NUMBERED.get(prefix)
    return Obs(name, kind) if kind and rest else None


EXECUTE_SNAPSHOTS: dict[str, type[BaseModel]] = {
    "propose_price_change": PriceChangeSnapshot,
    "propose_promotion": PromotionSnapshot,
    "propose_reorder": ReorderSnapshot,
    "request_review": ReviewSnapshot,
    "match_listing": MatchSnapshot,
    "notify_merchandiser": NotifyOutput,
}
