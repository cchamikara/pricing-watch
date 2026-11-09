"""Rules repricer and action executor: the baseline the agent is compared against.

The repricer holds each price within a band of the cheapest plausible competitor price, never
below the margin floor and never more than the policy's daily step from the start-of-day price.
Call it after a competitor publish; the executor writes the authoritative snapshot (S3).
"""

from datetime import datetime

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from pricing_watch.models import Action, CompetitorPrice, MarkdownLedger, Product, Proposal
from pricing_watch.money import ceil_div, floor_price
from pricing_watch.policy.config import PolicyConfig
from pricing_watch.policy.decide import propose
from pricing_watch.policy.rules import DbStateView, IncidentContext, markdown_cost_cents
from pricing_watch.policy.schemas import parse_action
from pricing_watch.tracing import Tracer

BAND_PCT = (98, 102)  # target range as % of the cheapest plausible competitor price


def target_price(
    current: int, start_of_day: int, cheapest: int, floor: int, max_step_pct: int
) -> int | None:
    """New shelf price, or None to leave it. Band first, then the daily step, then the floor."""
    low, high = ceil_div(cheapest * BAND_PCT[0], 100), cheapest * BAND_PCT[1] // 100
    target = min(max(current, low), high)
    step_low = ceil_div(start_of_day * (100 - max_step_pct), 100)
    step_high = start_of_day * (100 + max_step_pct) // 100
    target = max(min(max(target, step_low), step_high), floor)
    return None if target == current else target


def latest_prices(s: Session, sku: str) -> list[CompetitorPrice]:
    """Each competitor's most recent row for the SKU, plausible or not."""
    latest = (
        select(CompetitorPrice.competitor_id, func.max(CompetitorPrice.published_at).label("at"))
        .where(CompetitorPrice.sku == sku)
        .group_by(CompetitorPrice.competitor_id)
        .subquery()
    )
    return list(
        s.scalars(
            select(CompetitorPrice)
            .join(
                latest,
                and_(
                    CompetitorPrice.competitor_id == latest.c.competitor_id,
                    CompetitorPrice.published_at == latest.c.at,
                ),
            )
            .where(CompetitorPrice.sku == sku)
            .order_by(CompetitorPrice.competitor_id)
        )
    )


def cheapest_plausible(s: Session, sku: str) -> int | None:
    """Lowest of each competitor's latest price for the SKU, skipping implausible ones."""
    return min((r.price_inc_gst_cents for r in latest_prices(s, sku) if r.plausible), default=None)


def execute(s: Session, tracer: Tracer, proposal: Proposal, t: datetime) -> Action:
    """Apply an allowed proposal, charge its markdown, and record what the store looked like."""
    with tracer.observation(f"execute:{proposal.tool}", "tool", proposal.args) as obs:
        if proposal.tool != "propose_price_change":
            raise ValueError(f"executor cannot run {proposal.tool!r} yet")
        # before the price changes: the cost is measured from the current price
        parsed = parse_action(proposal.tool, proposal.args)
        for category, cost in markdown_cost_cents(parsed, DbStateView(s, t)).items():
            row = s.get(MarkdownLedger, (category, t.date()))
            if row is None:
                row = MarkdownLedger(category=category, sim_day=t.date(), spent_cents=0)
                s.add(row)
            row.spent_cents += cost
        product = s.get_one(Product, proposal.args["sku"])
        new_price = int(proposal.args["new_price_inc_gst_cents"])
        snapshot = {
            "cost_ex_gst_cents": product.cost_ex_gst_cents,
            "floor_price_inc_gst_cents": floor_price(
                product.cost_ex_gst_cents, product.margin_floor_pct
            ),
            "old_price_inc_gst_cents": product.price_inc_gst_cents,
            "new_price_inc_gst_cents": new_price,
        }
        product.price_inc_gst_cents = new_price
        action = Action(
            proposal_id=proposal.id,
            tool=proposal.tool,
            args=proposal.args,
            result=snapshot,
            executed_at=t,
        )
        s.add(action)
        s.flush()
        obs.set_output(snapshot)
    return action


def reprice(s: Session, tracer: Tracer, t: datetime, config: PolicyConfig) -> list[Action]:
    """Propose a band price for each SKU; policy decides, allowed ones execute now."""
    view = DbStateView(s, t)
    with tracer.observation("routine_reprice", "agent", {"sim_time": t.isoformat()}) as root:
        actions = []
        for product in s.scalars(select(Product).order_by(Product.sku)):
            if product.promo_lock_until and t < product.promo_lock_until:
                continue
            cheapest = cheapest_plausible(s, product.sku)
            if cheapest is None:
                continue
            new_price = target_price(
                product.price_inc_gst_cents,
                view.start_of_day_price(product.sku),
                cheapest,
                floor_price(product.cost_ex_gst_cents, product.margin_floor_pct),
                config.max_daily_step_pct,
            )
            if new_price is None:
                continue
            proposal, decision = propose(
                s, tracer, view, IncidentContext(), config, source="rules",
                tool="propose_price_change",
                args={"sku": product.sku, "new_price_inc_gst_cents": new_price},
                reason=f"cheapest plausible competitor price {cheapest}",
            )  # fmt: skip
            if decision.decision == "allowed":
                actions.append(execute(s, tracer, proposal, t))
        root.set_output({"actions_executed": len(actions)})
    return actions
