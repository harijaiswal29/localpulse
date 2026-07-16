"""ORM records. Every table carries client_id — multi-tenant scoping is in the
data model even while the deployment is single-tenant (P0 DoD)."""

from datetime import UTC, date, datetime

from sqlalchemy import JSON, Date, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from localpulse.data.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class ClientRecord(Base):
    __tablename__ = "clients"

    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_whatsapp: Mapped[str] = mapped_column(String(32), index=True, default="")
    context: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class DraftRecord(Base):
    __tablename__ = "drafts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    scheduled_for: Mapped[date | None] = mapped_column(Date, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ApprovalLogRecord(Base):
    __tablename__ = "approval_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    draft_id: Mapped[str] = mapped_column(String(64), index=True)
    from_state: Mapped[str] = mapped_column(String(32))
    to_state: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(64))
    note: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PublishLogRecord(Base):
    __tablename__ = "publish_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    draft_id: Mapped[str] = mapped_column(String(64), index=True)
    channel: Mapped[str] = mapped_column(String(32))
    external_ref: Mapped[str] = mapped_column(String(255))
    approval_log_id: Mapped[int] = mapped_column(Integer)  # auditability: publish -> approval
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MetricRecord(Base):
    __tablename__ = "metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    metric: Mapped[str] = mapped_column(String(64), index=True)
    value: Mapped[float] = mapped_column(Float)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReviewRecord(Base):
    """Reviews the Reputation Agent has seen — store minimally (spec §11 privacy)."""

    __tablename__ = "reviews"
    __table_args__ = (UniqueConstraint("client_id", "review_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    review_id: Mapped[str] = mapped_column(String(128), index=True)
    rating: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(String(16), default="en")
    author: Mapped[str] = mapped_column(String(128), default="")
    sentiment: Mapped[str] = mapped_column(String(16), default="positive")
    reply_draft_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CostLedgerRecord(Base):
    __tablename__ = "cost_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    category: Mapped[str] = mapped_column(String(32))
    amount_inr: Mapped[float] = mapped_column(Float)
    note: Mapped[str] = mapped_column(String(255), default="")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
