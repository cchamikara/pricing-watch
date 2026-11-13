"""Policy rules P1-P10. The model proposes; this code decides.

Rules read prices, costs, budgets and stock through a StateView backed by the simulator database,
never from values in the proposal. Evaluation is ordered: P1 and P2 validate the proposal, P3-P9
refuse on the first failure, and P10 runs only when everything else passed.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pricing_watch.models import Action, InboundOrder, MarkdownLedger, Product, StockLevel
from pricing_watch.money import Cents, discounted, ex_from_inc, floor_price
from pricing_watch.policy.config import PolicyConfig
from pricing_watch.policy.schemas import (
    ACTION_SCHEMAS,
    MatchListing,
    PriceChange,
    Promotion,
    Reorder,
    RequestReview,
    parse_action,
)
from pricing_watch.sim.promotions import active_discounts
from pricing_watch.sim.stock import forecast_daily_demand
from pricing_watch.tracing import contract
from pricing_watch.tracing.contract import DecisionKind


@dataclass(frozen=True)
class ProductFacts:
    sku: str
    category: str
    cost_ex_gst_cents: int
    price_inc_gst_cents: int
    margin_floor_pct: int
    promo_lock_until: datetime | None


class StateView(Protocol):
    @property
    def now(self) -> datetime: ...
    def product(self, sku: str) -> ProductFacts | None: ...
    def start_of_day_price(self, sku: str) -> int: ...
    def markdown_spent_cents(self, category: str) -> int: ...
    def forecast_daily(self, sku: str) -> float: ...
    def stock(self, sku: str) -> tuple[int, int]: ...  # (on hand, inbound)
    def discount_pct(self, sku: str) -> int: ...  # promotion in force now, 0 if none


@dataclass(frozen=True)
class IncidentContext:
    """Server-side facts about the incident, never taken from the model."""

    scope: frozenset[str] | None = None  # SKUs the incident may touch; None = any (rules path)
    suspect: frozenset[str] = frozenset()  # SKUs with suspect data in this incident
    listing_id: int | None = None  # the competitor listing a listing_match incident is about


@dataclass(frozen=True)
class Decision:
    decision: DecisionKind
    rule_ids: tuple[str, ...]
    detail: str
    checked: dict[str, Any]  # the values the rules compared against
    policy_version: str


@dataclass(frozen=True)
class RuleResult:
    ok: bool
    detail: str = ""
    checked: dict[str, Any] | None = None


Args = Any  # a parsed schema model from policy.schemas
Rule = Callable[[Args, StateView, IncidentContext, PolicyConfig], RuleResult]


def _skus(args: Args) -> list[str]:
    if isinstance(args, Promotion):
        return list(args.skus)
    if isinstance(args, MatchListing):
        return [args.sku] if args.sku else []
    return [args.sku] if isinstance(args, (PriceChange, Reorder, RequestReview)) else []


def _new_prices(args: Args, view: StateView) -> dict[str, int]:
    """SKU -> shelf price the proposal would set; promotions round the discount in our favour."""
    if isinstance(args, PriceChange):
        return {args.sku: args.new_price_inc_gst_cents}
    if isinstance(args, Promotion):
        return {
            sku: discounted(_facts(view, sku).price_inc_gst_cents, args.discount_pct)
            for sku in args.skus
        }
    return {}


def _facts(view: StateView, sku: str) -> ProductFacts:
    facts = view.product(sku)
    assert facts is not None, "P3 guarantees the SKU exists"
    return facts


def p3_sku_exists(args: Args, view: StateView, ctx: IncidentContext, _: PolicyConfig) -> RuleResult:
    if isinstance(args, MatchListing) and args.listing_id != ctx.listing_id:
        return RuleResult(False, f"listing {args.listing_id} is outside this incident")
    for sku in _skus(args):
        if view.product(sku) is None:
            return RuleResult(False, f"unknown SKU {sku}")
        if ctx.scope is not None and sku not in ctx.scope:
            return RuleResult(False, f"{sku} is outside this incident")
    return RuleResult(True)


def p4_margin_floor(
    args: Args, view: StateView, _: IncidentContext, __: PolicyConfig
) -> RuleResult:
    checked = {}
    for sku, new in _new_prices(args, view).items():
        if isinstance(args, PriceChange):  # a running promotion applies on top of the new price
            new = discounted(new, view.discount_pct(sku))
        f = _facts(view, sku)
        floor = floor_price(f.cost_ex_gst_cents, f.margin_floor_pct)
        checked[f"floor:{sku}"] = floor
        if new < floor:
            return RuleResult(False, f"{sku} at {new} is below its floor {floor}", checked)
    return RuleResult(True, checked=checked)


def p5_promo_lock(args: Args, view: StateView, _: IncidentContext, __: PolicyConfig) -> RuleResult:
    for sku in _new_prices(args, view):
        lock = _facts(view, sku).promo_lock_until
        if lock and view.now < lock:
            return RuleResult(False, f"{sku} is promotion-locked until {lock}")
    return RuleResult(True)


def p6_daily_step(args: Args, view: StateView, _: IncidentContext, c: PolicyConfig) -> RuleResult:
    """Cumulative change against the start-of-day price. Promotions are temporary and skip it."""
    if not isinstance(args, PriceChange):
        return RuleResult(True)
    start = view.start_of_day_price(args.sku)
    checked = {f"start_of_day:{args.sku}": start}
    if abs(args.new_price_inc_gst_cents - start) * 100 > start * c.max_daily_step_pct:
        return RuleResult(False, f"more than {c.max_daily_step_pct}% from today's start", checked)
    return RuleResult(True, checked=checked)


def markdown_cost_cents(args: Args, view: StateView) -> dict[str, int]:
    """Category -> markdown cost (S5): ex-GST cut x forecast units.

    The cut is measured from the lower of the start-of-day and current price, so a SKU cut twice in
    one day is charged once per cent. Going back up refunds nothing, and a cut after a raise is
    charged again: it over-counts, which only errs toward refusing.
    """
    days = 1.0
    if isinstance(args, Promotion):
        days = (args.end - args.start) / timedelta(days=1)
    costs: dict[str, int] = {}
    for sku, new in _new_prices(args, view).items():
        reference = min(view.start_of_day_price(sku), _facts(view, sku).price_inc_gst_cents)
        cut = ex_from_inc(Cents(reference)) - ex_from_inc(Cents(new))
        category = _facts(view, sku).category
        units = view.forecast_daily(sku) * days
        costs[category] = costs.get(category, 0) + round(max(cut, 0) * units)
    return costs


def p7_markdown_budget(
    args: Args, view: StateView, _: IncidentContext, c: PolicyConfig
) -> RuleResult:
    checked: dict[str, Any] = {}
    for category, cost in markdown_cost_cents(args, view).items():
        spent, budget = view.markdown_spent_cents(category), c.markdown_budget_cents[category]
        checked |= {f"markdown_cost:{category}": cost, f"spent:{category}": spent,
                    f"budget:{category}": budget}  # fmt: skip
        if spent + cost > budget:
            return RuleResult(False, f"{category} markdown {spent}+{cost} over {budget}", checked)
    return RuleResult(True, checked=checked)


def p8_reorder_cap(args: Args, view: StateView, _: IncidentContext, c: PolicyConfig) -> RuleResult:
    if not isinstance(args, Reorder):
        return RuleResult(True)
    on_hand, inbound = view.stock(args.sku)
    forecast = view.forecast_daily(args.sku)
    cover = (on_hand + inbound + args.qty) / forecast if forecast > 0 else float("inf")
    checked = {f"cover_after:{args.sku}": cover}
    if cover > c.max_days_of_cover:
        return RuleResult(False, f"cover {cover:.0f} days over {c.max_days_of_cover}", checked)
    return RuleResult(True, checked=checked)


def p9_suspect_data(args: Args, _: StateView, ctx: IncidentContext, __: PolicyConfig) -> RuleResult:
    if isinstance(args, (PriceChange, Promotion, Reorder)):
        for sku in _skus(args):
            if sku in ctx.suspect:
                return RuleResult(False, f"{sku} has suspect data in this incident")
    return RuleResult(True)


def p10_approval_tier(
    args: Args, view: StateView, _: IncidentContext, c: PolicyConfig
) -> RuleResult:
    """ok=False here means 'needs approval', not 'refused'."""
    if isinstance(args, Promotion):
        return RuleResult(args.discount_pct <= c.auto_apply_pct, f"{args.discount_pct}% discount")
    if isinstance(args, PriceChange):
        current = _facts(view, args.sku).price_inc_gst_cents
        change = abs(args.new_price_inc_gst_cents - current)
        return RuleResult(
            change * 100 <= current * c.auto_apply_pct, f"change {change} on {current}"
        )
    return RuleResult(True)


ORDERED_RULES: list[tuple[str, Rule]] = [
    ("P3-sku-exists", p3_sku_exists),
    ("P4-margin-floor", p4_margin_floor),
    ("P5-promo-lock", p5_promo_lock),
    ("P6-daily-step", p6_daily_step),
    ("P7-markdown-budget", p7_markdown_budget),
    ("P8-reorder-cap", p8_reorder_cap),
    ("P9-suspect-data", p9_suspect_data),
]


def evaluate(
    tool: str,
    args: dict[str, Any],
    view: StateView,
    ctx: IncidentContext,
    config: PolicyConfig,
    *,
    skip_approval: bool = False,
) -> Decision:
    """Decide one proposal. skip_approval re-checks P1-P9 only, for an approval being executed."""
    version = str(config.policy_version)
    if tool not in ACTION_SCHEMAS:
        return Decision("refused", ("P1-allowlist",), f"{tool!r} is not an action", {}, version)
    try:
        parsed = parse_action(tool, args)
    except ValueError as e:
        return Decision("refused", ("P2-schema",), str(e)[:500], {}, version)
    checked: dict[str, Any] = {}
    for rule_id, rule in ORDERED_RULES:
        result = rule(parsed, view, ctx, config)
        checked |= result.checked or {}
        if not result.ok:
            return Decision("refused", (rule_id,), result.detail, checked, version)
    if not skip_approval:
        tier = p10_approval_tier(parsed, view, ctx, config)
        if not tier.ok:
            return Decision("needs_approval", ("P10-approval-tier",), tier.detail, checked, version)
    return Decision("allowed", (), "", checked, version)


class DbStateView:
    """StateView over the simulator database at time t."""

    def __init__(self, s: Session, t: datetime) -> None:
        self._s, self._t = s, t

    @property
    def now(self) -> datetime:
        return self._t

    def product(self, sku: str) -> ProductFacts | None:
        p = self._s.get(Product, sku)
        if p is None:
            return None
        return ProductFacts(p.sku, p.category, p.cost_ex_gst_cents, p.price_inc_gst_cents,
                            p.margin_floor_pct, p.promo_lock_until)  # fmt: skip

    def start_of_day_price(self, sku: str) -> int:
        """The old price on today's first price change, or the current price if none yet."""
        day_start = datetime.combine(self._t.date(), datetime.min.time())
        first = self._s.scalars(
            select(Action)
            .where(Action.tool == "propose_price_change", Action.executed_at >= day_start)
            .where(Action.args["sku"].as_string() == sku)
            .order_by(Action.executed_at, Action.id)
            .limit(1)
        ).first()
        if first is not None:
            return contract.PriceChangeSnapshot.model_validate(first.result).old_price_inc_gst_cents
        return _facts(self, sku).price_inc_gst_cents

    def markdown_spent_cents(self, category: str) -> int:
        row = self._s.get(MarkdownLedger, (category, self._today()))
        return row.spent_cents if row else 0

    def forecast_daily(self, sku: str) -> float:
        return forecast_daily_demand(self._s, sku, self._t)

    def discount_pct(self, sku: str) -> int:
        return active_discounts(self._s, self._t).get(sku, 0)

    def stock(self, sku: str) -> tuple[int, int]:
        level = self._s.get(StockLevel, sku)
        inbound = self._s.scalar(
            select(func.coalesce(func.sum(InboundOrder.qty), 0)).where(InboundOrder.sku == sku)
        )
        return (level.on_hand if level else 0), int(inbound or 0)

    def _today(self) -> date:
        return self._t.date()
