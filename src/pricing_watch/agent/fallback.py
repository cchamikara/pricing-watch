"""Rules fallback: a deterministic plan per incident kind, for when the model can't answer.

It has the shape of a model reply, so the loop sends it through policy exactly like the model's.
"""

import math
from datetime import timedelta

from pricing_watch.agent.model import Brief
from pricing_watch.agent.output import MessageOut, ModelOutput, ProposalOut
from pricing_watch.agent.tools import CompetitorRead, PolicyRead
from pricing_watch.policy.rules import StateView, markdown_cost_cents
from pricing_watch.policy.schemas import Promotion, Severity
from pricing_watch.sim.repricer import target_price

EXTRA_COVER_DAYS = 14  # reorder to the lead time plus this many days of forecast demand
PROMO_PCT, PROMO_DAYS = 5, 2  # small enough to auto-apply (P10)


def rules_plan(
    b: Brief, competitors: CompetitorRead | None, policy: PolicyRead, view: StateView
) -> ModelOutput:
    scope = b.sku or b.category

    def plan(severity: Severity, text: str, *proposals: ProposalOut) -> ModelOutput:
        return ModelOutput(diagnosis=f"rules fallback for {b.kind} on {scope}", confidence="low",
                           proposals=list(proposals),
                           message=MessageOut(severity=severity, text=text))  # fmt: skip

    def review(why: str) -> ProposalOut:
        return ProposalOut(tool="request_review", args={"sku": b.sku, "reason": why}, reason=why)

    if b.kind == "listing_match" and b.candidates:  # rules can't judge a title: a person must
        why = f"Is competitor listing {b.listing_id} our {b.candidates[0][0]}?"
        return plan("action_needed", why, ProposalOut(
            tool="request_review", reason="unsure of the match",
            args={"sku": b.candidates[0][0], "reason": why}))  # fmt: skip
    if b.sku is None or b.product is None or b.stock is None:  # category-wide: margin_erosion
        return plan("action_needed", f"{b.kind} on {scope}: please review prices and costs.")
    if b.kind == "supplier_notice":  # rules can't read text, so a person must
        return plan("action_needed", f"{b.sku}: a supplier notice arrived; please read it.")
    if b.kind == "suspect_feed" or b.suspect:  # P9 would refuse any price or reorder anyway
        why = "Feed or stock data looks implausible."
        return plan("action_needed", f"{b.sku}: {why} Asked for a review.", review(why))
    p = b.product
    locked = p.promo_lock_until is not None and p.promo_lock_until > b.now  # no price moves (P5)

    if b.kind == "stockout_risk":
        days = p.lead_time_hours / 24 + EXTRA_COVER_DAYS
        qty = math.ceil(b.forecast_daily * days) - b.stock.on_hand - b.stock.inbound_qty
        if qty <= 0:  # the trigger fired anyway: stock on order lands after the shelf empties
            why = "runs out before the stock on order arrives; a new order would land no sooner."
            return plan("action_needed", f"{b.sku} {why} Please expedite it.")
        args = {"sku": b.sku, "qty": qty}
        reorder = ProposalOut(tool="propose_reorder", args=args, reason=f"cover {days:g} days")
        return plan("warn", f"{b.sku}: reordering {qty} to avoid a stock-out.", reorder)

    competitor_moved = b.kind in ("competitor_undercut", "competitor_price_drop")
    if competitor_moved and locked:
        return plan("warn", f"{b.sku}: {b.kind}, but its promotion lock holds the price.")
    if competitor_moved:  # rules can't read notes: follow the competitor like the repricer
        cheapest = competitors.cheapest_fresh(b.now) if competitors else None
        new = cheapest and target_price(p.price_inc_gst_cents, view.start_of_day_price(b.sku),
                                        cheapest, p.floor_price_inc_gst_cents,
                                        policy.max_daily_step_pct)  # fmt: skip
        if not new:
            return plan("warn", f"{b.sku}: {b.kind}, but the price is as low as the rules allow.")
        args = {"sku": b.sku, "new_price_inc_gst_cents": new}
        cut = ProposalOut(tool="propose_price_change", args=args, reason=f"cheapest {cheapest}")
        return plan("warn", f"{b.sku}: {b.kind}; repricing to {new / 100:.2f}.", cut)

    if b.kind == "slow_mover":
        promo = Promotion(skus=[b.sku], discount_pct=PROMO_PCT, start=b.now,
                          end=b.now + timedelta(days=PROMO_DAYS))  # fmt: skip
        cost = markdown_cost_cents(promo, view).get(b.category, 0)
        if not locked and cost <= policy.budget_remaining_cents.get(b.category, 0):
            args = promo.model_dump(mode="json")
            promotion = ProposalOut(tool="propose_promotion", args=args, reason="slow seller")
            return plan(
                "info", f"{b.sku}: a {PROMO_PCT}% promotion for {PROMO_DAYS} days.", promotion
            )
        why = "Slow seller; a promotion is locked or over today's budget."
        return plan("action_needed", f"{b.sku}: {why}", review(why))

    return plan("action_needed", f"{b.kind} on {scope}: no rules plan; please review.")
