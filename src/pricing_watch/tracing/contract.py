"""The trace contract: every observation name and type, attribute key and output shape that the
emitter writes and the evaluators read. Nothing else spells them out (a grep test checks), so a
rename here reaches both sides at once.

Each observation is an `Obs(name, type)`; emitters call `tracer.observation(*contract.X, input)`,
so a name can't be paired with the wrong type. Attributes and outputs are Pydantic models;
emitters pass `Model(...).model_dump(mode="json")`, so a misspelt key fails type checking.
"""

from typing import Any, Literal, NamedTuple

from pydantic import BaseModel

from pricing_watch.tracing import ObservationType


class Obs(NamedTuple):
    name: str
    type: ObservationType


# roots
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


# attributes


class IncidentAttributes(BaseModel):
    incident_kind: str
    category: str
    sku: str | None
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


class PriceChangeSnapshot(BaseModel):
    cost_ex_gst_cents: int
    floor_price_inc_gst_cents: int
    old_price_inc_gst_cents: int
    new_price_inc_gst_cents: int
    markdown_spent_cents: dict[str, SpendDelta]  # by category


class PromotedSku(BaseModel):
    cost_ex_gst_cents: int
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
