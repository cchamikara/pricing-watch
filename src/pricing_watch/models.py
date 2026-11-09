"""Simulator and agent state in SQLite. Langfuse holds traces and scores; nothing here copies them.

Money is integer cents. Times are simulated times from the SimClock (naive, store-local).
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Engine, ForeignKey, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[str]: JSON}


class Product(Base):
    __tablename__ = "product"
    sku: Mapped[str] = mapped_column(primary_key=True)
    category: Mapped[str]
    cost_ex_gst_cents: Mapped[int]
    price_inc_gst_cents: Mapped[int]
    margin_floor_pct: Mapped[int]  # whole percent, so floor checks stay in integer maths
    lead_time_hours: Mapped[int]
    supplier_description: Mapped[str]
    promo_lock_until: Mapped[datetime | None]


class StockLevel(Base):
    __tablename__ = "stock_level"
    sku: Mapped[str] = mapped_column(ForeignKey("product.sku"), primary_key=True)
    on_hand: Mapped[int]


class InboundOrder(Base):
    __tablename__ = "inbound_order"
    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(ForeignKey("product.sku"))
    qty: Mapped[int]
    eta: Mapped[datetime]


class CompetitorPrice(Base):
    __tablename__ = "competitor_price"
    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_id: Mapped[str]
    sku: Mapped[str] = mapped_column(ForeignKey("product.sku"), index=True)
    price_inc_gst_cents: Mapped[int]
    published_at: Mapped[datetime]
    plausible: Mapped[bool]


class Incident(Base):
    __tablename__ = "incident"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str]
    category: Mapped[str]
    sku: Mapped[str | None] = mapped_column(ForeignKey("product.sku"))  # None: category-wide
    suspect_skus: Mapped[list[str]] = mapped_column(default=list)  # flagged when it opened (P9)
    opened_at: Mapped[datetime]
    closed_at: Mapped[datetime | None]
    trace_id: Mapped[str]
    session_id: Mapped[str]
    answer_source: Mapped[str]  # "model" or "rules_fallback"


class Proposal(Base):
    __tablename__ = "proposal"
    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incident.id"))  # None on rules path
    source: Mapped[str]  # "model" or "rules"
    tool: Mapped[str]
    args: Mapped[dict[str, Any]]
    reason: Mapped[str]


class PolicyDecision(Base):
    __tablename__ = "policy_decision"
    proposal_id: Mapped[int] = mapped_column(ForeignKey("proposal.id"), primary_key=True)
    decision: Mapped[str]  # "allowed", "needs_approval" or "refused"
    rule_ids: Mapped[list[str]]
    detail: Mapped[str]
    policy_version: Mapped[str]


class Action(Base):
    __tablename__ = "action"
    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("proposal.id"))
    tool: Mapped[str]
    args: Mapped[dict[str, Any]]
    result: Mapped[dict[str, Any]]  # includes the executor's authoritative snapshot (S3)
    executed_at: Mapped[datetime]


class Approval(Base):
    __tablename__ = "approval"
    proposal_id: Mapped[int] = mapped_column(ForeignKey("proposal.id"), primary_key=True)
    status: Mapped[str]  # "pending", "approved" or "rejected"
    reviewer: Mapped[str]  # a person, or "script"
    decided_at: Mapped[datetime | None]
    note: Mapped[str]


class Notification(Base):
    __tablename__ = "notification"
    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incident.id"))
    severity: Mapped[str]
    message: Mapped[str]
    sent_at: Mapped[datetime]


class SalesTick(Base):
    __tablename__ = "sales_tick"
    sku: Mapped[str] = mapped_column(ForeignKey("product.sku"), primary_key=True)
    sim_time: Mapped[datetime] = mapped_column(primary_key=True)
    units_sold: Mapped[int]
    lost_units: Mapped[int]
    revenue_ex_gst_cents: Mapped[int]
    cogs_cents: Mapped[int]


class MarkdownLedger(Base):
    __tablename__ = "markdown_ledger"
    category: Mapped[str] = mapped_column(primary_key=True)
    sim_day: Mapped[date] = mapped_column(primary_key=True)
    spent_cents: Mapped[int]


def make_engine(url: str = "sqlite://") -> Engine:
    """In-memory SQLite by default; pass a file URL for runs. Creates all tables."""
    engine = create_engine(url)

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(conn: Any, _record: Any) -> None:
        conn.execute("PRAGMA foreign_keys=ON")  # SQLite ignores foreign keys unless asked

    Base.metadata.create_all(engine)
    return engine
