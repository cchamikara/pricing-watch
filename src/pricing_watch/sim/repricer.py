"""Rules repricer and action executor: the baseline the agent is compared against.

The repricer holds each price within a band of the cheapest plausible competitor price, never
below the margin floor and never more than MAX_DAILY_STEP from the start-of-day price.
Call it after a competitor publish; the executor writes the authoritative snapshot (S3).
"""

from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from pricing_watch.models import Action, CompetitorPrice, Product, Proposal
from pricing_watch.tracing import Tracer

BAND_PCT = (98, 102)  # target range as % of the cheapest plausible competitor price
MAX_DAILY_STEP_PCT = 20


def _ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def floor_price(cost_ex_gst_cents: int, margin_floor_pct: int) -> int:
    """Lowest shelf price (inc GST) whose ex-GST price meets cost x (1 + floor).

    Solves ex_from_inc(p) >= x exactly under half-up rounding: p >= 1.1x - 0.55.
    """
    min_ex = _ceil_div(cost_ex_gst_cents * (100 + margin_floor_pct), 100)
    return _ceil_div(22 * min_ex - 11, 20)


def target_price(current: int, start_of_day: int, cheapest: int, floor: int) -> int | None:
    """New shelf price, or None to leave it. Band first, then the daily step, then the floor."""
    low, high = _ceil_div(cheapest * BAND_PCT[0], 100), cheapest * BAND_PCT[1] // 100
    target = min(max(current, low), high)
    step_low = _ceil_div(start_of_day * (100 - MAX_DAILY_STEP_PCT), 100)
    step_high = start_of_day * (100 + MAX_DAILY_STEP_PCT) // 100
    target = max(min(max(target, step_low), step_high), floor)
    return None if target == current else target


def cheapest_plausible(s: Session, sku: str) -> int | None:
    """Lowest of each competitor's latest price for the SKU, skipping implausible ones."""
    latest = (
        select(CompetitorPrice.competitor_id, func.max(CompetitorPrice.published_at).label("at"))
        .where(CompetitorPrice.sku == sku)
        .group_by(CompetitorPrice.competitor_id)
        .subquery()
    )
    return s.scalar(
        select(func.min(CompetitorPrice.price_inc_gst_cents))
        .join(
            latest,
            and_(
                CompetitorPrice.competitor_id == latest.c.competitor_id,
                CompetitorPrice.published_at == latest.c.at,
            ),
        )
        .where(CompetitorPrice.sku == sku, CompetitorPrice.plausible)
    )


def execute(s: Session, tracer: Tracer, proposal: Proposal, t: datetime) -> Action:
    """Apply an allowed proposal and record what the store looked like when it did."""
    with tracer.observation(f"execute:{proposal.tool}", "tool", proposal.args) as obs:
        if proposal.tool != "propose_price_change":
            raise ValueError(f"executor cannot run {proposal.tool!r} yet")
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


def reprice(
    s: Session, tracer: Tracer, t: datetime, start_of_day: Mapping[str, int]
) -> list[Action]:
    with tracer.observation("routine_reprice", "agent", {"sim_time": t.isoformat()}) as root:
        actions = []
        for product in s.scalars(select(Product).order_by(Product.sku)):
            if product.promo_lock_until and t < product.promo_lock_until:
                continue
            cheapest = cheapest_plausible(s, product.sku)
            if cheapest is None:
                continue
            floor = floor_price(product.cost_ex_gst_cents, product.margin_floor_pct)
            new_price = target_price(
                product.price_inc_gst_cents, start_of_day[product.sku], cheapest, floor
            )
            if new_price is None:
                continue
            proposal = Proposal(
                incident_id=None,
                source="rules",
                tool="propose_price_change",
                args={"sku": product.sku, "new_price_inc_gst_cents": new_price},
                reason=f"cheapest plausible competitor price {cheapest}",
            )
            s.add(proposal)
            s.flush()
            actions.append(execute(s, tracer, proposal, t))
        root.set_output({"actions_executed": len(actions)})
    return actions
