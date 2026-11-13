"""Read tools (S5): what the agent sees, after any observation faults. Each read is a `tool` span.

`read_skus` collects every SKU read so far, for the `reads_before_acting` check.
"""

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from pricing_watch.models import CompetitorListing, CompetitorPrice, Product, SupplierNotice
from pricing_watch.money import floor_price
from pricing_watch.policy.config import PolicyConfig
from pricing_watch.policy.rules import DbStateView
from pricing_watch.sim.faults import Fault, observed_description, observed_stock
from pricing_watch.sim.matching import candidates
from pricing_watch.sim.repricer import latest_prices
from pricing_watch.sim.stock import FORECAST_WINDOW, days_of_cover
from pricing_watch.tracing import Tracer

NOTICE_WINDOW = timedelta(days=14)  # supplier notices older than this are left out


class ProductRead(BaseModel):
    sku: str
    category: str
    cost_ex_gst_cents: int
    price_inc_gst_cents: int
    margin_floor_pct: int
    floor_price_inc_gst_cents: int
    lead_time_hours: int
    promo_lock_until: datetime | None
    supplier_description: str  # untrusted text; the prompt wraps it as data


class StockRead(BaseModel):
    sku: str
    on_hand: int
    inbound_qty: int
    days_of_cover: float | None  # on hand only; None when there is no forecast


class CompetitorQuote(BaseModel):
    competitor_id: str
    price_inc_gst_cents: int
    published_at: datetime
    age_hours: int
    plausible: bool
    note: str  # the listing's free text; untrusted, so the prompt wraps it as data


class CompetitorRead(BaseModel):
    sku: str
    prices: list[CompetitorQuote]


class ForecastRead(BaseModel):
    sku: str
    daily_units: float
    window_days: int


class NoticeQuote(BaseModel):
    sku: str | None
    category: str | None  # set when the notice covers a whole range
    sent_at: datetime
    age_hours: int
    text: str  # untrusted; the prompt wraps it as data


class SupplierNoticesRead(BaseModel):
    category: str
    sku: str | None
    notices: list[NoticeQuote]  # oldest first


class Candidate(BaseModel):
    sku: str
    title: str
    mpn: str
    price_inc_gst_cents: int
    similarity: float  # how alike the titles are, 0 to 1


class ListingRead(BaseModel):
    listing_id: int
    competitor_id: str
    listing_code: str
    title: str  # untrusted; the prompt wraps it as data
    mpn: str | None
    price_inc_gst_cents: int | None  # its latest price
    candidates: list[Candidate]  # our closest products, best first


class PolicyRead(BaseModel):
    policy_version: str
    auto_apply_pct: int
    max_daily_step_pct: int
    max_days_of_cover: int
    budget_remaining_cents: dict[str, int]  # today's markdown budget left, per category


class ReadTools:
    def __init__(
        self, s: Session, tracer: Tracer, t: datetime, config: PolicyConfig,
        faults: Sequence[Fault] = (),
    ) -> None:  # fmt: skip
        self._s, self._tracer, self._t, self._config, self._faults = s, tracer, t, config, faults
        self._view = DbStateView(s, t)
        self.read_skus: set[str] = set()

    def _traced[M: BaseModel](self, name: str, sku: str | None, read: Callable[[], M]) -> M:
        """Run the read inside its span, so the span times it and records a failure."""
        with self._tracer.observation(name, "tool", {"sku": sku} if sku else {}) as obs:
            out = read()
            obs.set_output(out.model_dump(mode="json"))
        if sku:
            self.read_skus.add(sku)
        return out

    def get_product(self, sku: str) -> ProductRead:
        def read() -> ProductRead:
            p = self._s.get_one(Product, sku)
            return ProductRead(
                sku=sku, category=p.category, cost_ex_gst_cents=p.cost_ex_gst_cents,
                price_inc_gst_cents=p.price_inc_gst_cents, margin_floor_pct=p.margin_floor_pct,
                floor_price_inc_gst_cents=floor_price(p.cost_ex_gst_cents, p.margin_floor_pct),
                lead_time_hours=p.lead_time_hours, promo_lock_until=p.promo_lock_until,
                supplier_description=observed_description(p.supplier_description, sku,
                                                          self._faults, self._t),
            )  # fmt: skip

        return self._traced("get_product", sku, read)

    def get_stock(self, sku: str) -> StockRead:
        def read() -> StockRead:
            book, inbound = self._view.stock(sku)
            on_hand = observed_stock(book, sku, self._faults, self._t)
            forecast = self._view.forecast_daily(sku)
            cover = days_of_cover(on_hand, forecast) if forecast > 0 else None
            return StockRead(sku=sku, on_hand=on_hand, inbound_qty=inbound, days_of_cover=cover)

        return self._traced("get_stock", sku, read)

    def get_competitor_prices(self, sku: str) -> CompetitorRead:
        def read() -> CompetitorRead:
            quotes = [
                CompetitorQuote(competitor_id=r.competitor_id, published_at=r.published_at,
                                price_inc_gst_cents=r.price_inc_gst_cents, plausible=r.plausible,
                                age_hours=(self._t - r.published_at) // timedelta(hours=1),
                                note=r.note)
                for r in latest_prices(self._s, sku)
            ]  # fmt: skip
            return CompetitorRead(sku=sku, prices=quotes)

        return self._traced("get_competitor_prices", sku, read)

    def get_demand_forecast(self, sku: str) -> ForecastRead:
        return self._traced("get_demand_forecast", sku, lambda: ForecastRead(
            sku=sku, daily_units=self._view.forecast_daily(sku), window_days=FORECAST_WINDOW.days
        ))  # fmt: skip

    def get_supplier_notices(self, category: str, sku: str | None = None) -> SupplierNoticesRead:
        """Notices sent in the last NOTICE_WINDOW for the range, and for the SKU if given."""

        def read() -> SupplierNoticesRead:
            scope = SupplierNotice.category == category
            if sku:
                scope |= SupplierNotice.sku == sku
            else:  # a range incident: notices about any SKU in it count too
                in_range = select(Product.sku).where(Product.category == category)
                scope |= SupplierNotice.sku.in_(in_range)
            rows = self._s.scalars(
                select(SupplierNotice)
                .where(scope, SupplierNotice.sent_at <= self._t,
                       SupplierNotice.sent_at > self._t - NOTICE_WINDOW)
                .order_by(SupplierNotice.sent_at, SupplierNotice.id)
            )  # fmt: skip
            notices = [
                NoticeQuote(sku=n.sku, category=n.category, sent_at=n.sent_at, text=n.text,
                            age_hours=(self._t - n.sent_at) // timedelta(hours=1))
                for n in rows
            ]  # fmt: skip
            return SupplierNoticesRead(category=category, sku=sku, notices=notices)

        return self._traced("get_supplier_notices", sku, read)

    def get_listing(self, listing_id: int) -> ListingRead:
        def read() -> ListingRead:
            li = self._s.get_one(CompetitorListing, listing_id)
            price = self._s.scalars(
                select(CompetitorPrice.price_inc_gst_cents)
                .where(CompetitorPrice.listing_id == listing_id)
                .order_by(CompetitorPrice.published_at.desc()).limit(1)
            ).first()  # fmt: skip
            near = [Candidate(sku=p.sku, title=p.title, mpn=p.mpn, similarity=round(score, 2),
                              price_inc_gst_cents=p.price_inc_gst_cents)
                    for score, p in candidates(self._s, li.title)]  # fmt: skip
            return ListingRead(listing_id=li.id, competitor_id=li.competitor_id, title=li.title,
                               listing_code=li.listing_code, mpn=li.mpn,
                               price_inc_gst_cents=price, candidates=near)  # fmt: skip

        return self._traced("get_listing", None, read)

    def get_policy(self) -> PolicyRead:
        def read() -> PolicyRead:
            c = self._config
            remaining = {cat: budget - self._view.markdown_spent_cents(cat)
                         for cat, budget in c.markdown_budget_cents.items()}  # fmt: skip
            return PolicyRead(
                policy_version=c.policy_version.isoformat(),
                auto_apply_pct=c.auto_apply_pct,
                max_daily_step_pct=c.max_daily_step_pct,
                max_days_of_cover=c.max_days_of_cover,
                budget_remaining_cents=remaining,
            )

        return self._traced("get_policy", None, read)
