"""Rules repricer and action executor: the baseline the agent is compared against.

The repricer holds each price within a band of the cheapest plausible competitor price, never
below the margin floor and never more than the policy's daily step from the start-of-day price.
Call it after a competitor publish; the executor writes the authoritative snapshot (S3).
"""

from collections.abc import Collection
from datetime import datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from pricing_watch.models import (
    Action,
    CompetitorListing,
    CompetitorPrice,
    MarkdownLedger,
    Notification,
    Product,
    Proposal,
    ScheduledDiscount,
)
from pricing_watch.money import ceil_div, discounted, floor_price
from pricing_watch.policy.config import PolicyConfig
from pricing_watch.policy.decide import propose
from pricing_watch.policy.rules import DbStateView, IncidentContext, markdown_cost_cents
from pricing_watch.policy.schemas import (
    MatchListing,
    NotifyMerchandiser,
    PriceChange,
    Promotion,
    Reorder,
    RequestReview,
    parse_action,
)
from pricing_watch.sim.keywords import looks_temporary
from pricing_watch.sim.stock import reorder
from pricing_watch.tracing import Tracer, contract

BAND_PCT = (98, 102)  # target range as % of the cheapest plausible competitor price
DROP_PCT = 10  # a competitor price this far under its 7-day high is a sharp drop
DROP_WINDOW = timedelta(days=7)


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


def cheapest_plausible(s: Session, sku: str, skip_temporary: bool = False) -> int | None:
    """Lowest of each competitor's latest price for the SKU, skipping implausible ones, and with
    skip_temporary, those whose note looks like a short sale."""
    return min(
        (r.price_inc_gst_cents for r in latest_prices(s, sku)
         if r.plausible and not (skip_temporary and looks_temporary(r.note))),
        default=None,
    )  # fmt: skip


def execute(
    s: Session, tracer: Tracer, proposal: Proposal, t: datetime, config: PolicyConfig
) -> Action:
    """Apply a proposal policy allowed, and record what the store looked like (S3).

    A proposed `notify_merchandiser` is sent here; each incident's closing notice is sent by the
    incident loop instead, so it can never be refused.
    """
    with tracer.observation(*contract.execute(proposal.tool), proposal.args) as obs:
        args = parse_action(proposal.tool, proposal.args)
        view = DbStateView(s, t)
        snapshot: contract.ExecuteSnapshot
        if isinstance(args, PriceChange):
            spent = _charge_markdown(s, args, view, t, config)  # first: cut from the old price
            product = s.get_one(Product, args.sku)
            snapshot = contract.PriceChangeSnapshot(
                cost_ex_gst_cents=product.cost_ex_gst_cents,
                margin_floor_pct=product.margin_floor_pct,
                floor_price_inc_gst_cents=_floor(product),
                running_discount_pct=view.discount_pct(args.sku),
                old_price_inc_gst_cents=product.price_inc_gst_cents,
                new_price_inc_gst_cents=args.new_price_inc_gst_cents, markdown_spent_cents=spent,
            )  # fmt: skip
            product.price_inc_gst_cents = args.new_price_inc_gst_cents
        elif isinstance(args, Promotion):
            spent = _charge_markdown(s, args, view, t, config)
            promoted = {}
            for sku in args.skus:
                product = s.get_one(Product, sku)
                promoted[sku] = contract.PromotedSku(
                    cost_ex_gst_cents=product.cost_ex_gst_cents,
                    margin_floor_pct=product.margin_floor_pct,
                    floor_price_inc_gst_cents=_floor(product),
                    price_inc_gst_cents=product.price_inc_gst_cents,
                    promo_price_inc_gst_cents=discounted(product.price_inc_gst_cents,
                                                         args.discount_pct),
                )  # fmt: skip
                s.add(ScheduledDiscount(proposal_id=proposal.id, sku=sku,
                                        discount_pct=args.discount_pct,
                                        start=args.start, end=args.end))  # fmt: skip
            snapshot = contract.PromotionSnapshot(skus=promoted, markdown_spent_cents=spent)
        elif isinstance(args, Reorder):
            on_hand, inbound = view.stock(args.sku)
            order = reorder(s, args.sku, args.qty, t)
            snapshot = contract.ReorderSnapshot(
                on_hand=on_hand, inbound_before=inbound, qty=args.qty,
                eta=order.eta.isoformat(), forecast_daily=view.forecast_daily(args.sku),
            )  # fmt: skip
        elif isinstance(args, RequestReview):  # the Action row is the review request
            snapshot = contract.ReviewSnapshot(
                price_inc_gst_cents=s.get_one(Product, args.sku).price_inc_gst_cents,
                on_hand=view.stock(args.sku)[0],
            )
        elif isinstance(args, MatchListing):  # the model judged which product a listing is
            listing = s.get_one(CompetitorListing, args.listing_id)
            snapshot = contract.MatchSnapshot(title=listing.title, matched_sku=args.sku,
                                              was=listing.matched_sku)  # fmt: skip
            listing.matched_sku, listing.matched_by = args.sku, "model"
            listing.match_confidence = 1.0 if args.sku else 0.0
            for row in s.scalars(
                select(CompetitorPrice).where(CompetitorPrice.listing_id == listing.id)
            ):
                row.sku = args.sku  # its prices now count for that product, or for none
        else:
            assert isinstance(args, NotifyMerchandiser)
            if proposal.incident_id is None:
                raise ValueError("a notification needs an incident")
            note = Notification(incident_id=proposal.incident_id, severity=args.severity,
                                message=args.message, sent_at=t)  # fmt: skip
            s.add(note)
            s.flush()
            snapshot = contract.NotifyOutput(notification_id=note.id)
        result = snapshot.model_dump(mode="json")
        action = Action(proposal_id=proposal.id, tool=proposal.tool, args=proposal.args,
                        result=result, executed_at=t)  # fmt: skip
        s.add(action)
        s.flush()
        obs.set_output(result)
    return action


def _floor(product: Product) -> int:
    return floor_price(product.cost_ex_gst_cents, product.margin_floor_pct)


def _charge_markdown(
    s: Session, args: PriceChange | Promotion, view: DbStateView, t: datetime, config: PolicyConfig
) -> dict[str, contract.SpendDelta]:
    """Add the markdown cost to today's ledger; returns spend before and after, per category."""
    spent = {}
    for category, cost in markdown_cost_cents(args, view).items():
        row = s.get(MarkdownLedger, (category, t.date()))
        if row is None:
            row = MarkdownLedger(category=category, sim_day=t.date(), spent_cents=0)
            s.add(row)
        spent[category] = contract.SpendDelta(
            before=row.spent_cents,
            after=row.spent_cents + cost,
            budget=config.markdown_budget_cents[category],
        )
        row.spent_cents += cost
    return spent


def dropped_skus(s: Session, t: datetime) -> set[str]:
    """SKUs where a competitor's latest plausible price is more than DROP_PCT under that
    competitor's own highest plausible price in the last DROP_WINDOW: a sale, or a real cut."""
    high = dict(
        s.execute(
            select(CompetitorPrice.competitor_id + "|" + CompetitorPrice.sku,
                   func.max(CompetitorPrice.price_inc_gst_cents))
            .where(CompetitorPrice.plausible, CompetitorPrice.sku.is_not(None),  # matched only
                   CompetitorPrice.published_at > t - DROP_WINDOW,
                   CompetitorPrice.published_at <= t)
            .group_by(CompetitorPrice.competitor_id, CompetitorPrice.sku)
        ).all()
    )  # fmt: skip
    return {
        sku
        for sku in {k.split("|", 1)[1] for k in high}
        for r in latest_prices(s, sku)
        if r.plausible
        and r.price_inc_gst_cents * 100 < high[f"{r.competitor_id}|{sku}"] * (100 - DROP_PCT)
    }


def reprice(
    s: Session,
    tracer: Tracer,
    t: datetime,
    config: PolicyConfig,
    *,
    text_rules: bool = False,
    defer: Collection[str] = frozenset(),
    run: contract.RunTrace | None = None,
) -> list[Action]:
    """Propose a band price for each SKU; policy decides, allowed ones execute now. With
    text_rules, competitor prices whose note looks temporary are not followed. SKUs in defer are
    left alone: in agent mode the agent decides on sharp competitor drops."""
    view = DbStateView(s, t)
    trace = contract.root_trace(run, "reprice", t.isoformat(timespec="minutes"), t,
                                str(config.policy_version))  # fmt: skip
    with tracer.observation(*contract.ROUTINE_REPRICE, {"sim_time": t.isoformat()},
                            trace=trace) as root:  # fmt: skip
        actions = []
        for product in s.scalars(select(Product).order_by(Product.sku)):
            if product.sku in defer or (product.promo_lock_until and t < product.promo_lock_until):
                continue
            cheapest = cheapest_plausible(s, product.sku, skip_temporary=text_rules)
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
                actions.append(execute(s, tracer, proposal, t, config))
        root.set_output(contract.RepriceOutput(actions_executed=len(actions)).model_dump())
    return actions
