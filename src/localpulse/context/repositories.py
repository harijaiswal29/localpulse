"""Repositories — the only way agents read/write state (golden rule #6).

Every repository except ClientRepository is bound to a single client_id at
construction time, so cross-tenant access is impossible by design (golden rule #3).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from localpulse.context.models import ApprovalState, ClientContext, DraftItem
from localpulse.data.models import (
    ApprovalLogRecord,
    ClientRecord,
    ConversationRecord,
    CostLedgerRecord,
    DraftRecord,
    EnquiryRecord,
    MetricRecord,
    PublishLogRecord,
    ReviewRecord,
)


class NotFoundError(Exception):
    """Entity does not exist — or belongs to another client (indistinguishable on purpose)."""


class ClientRepository:
    """Admin-level access to client contexts (not scoped: it is the tenant directory)."""

    def __init__(self, session: Session):
        self._session = session

    def save(self, context: ClientContext) -> None:
        record = self._session.get(ClientRecord, context.client_id)
        payload = context.model_dump(mode="json")
        if record is None:
            record = ClientRecord(
                client_id=context.client_id,
                owner_whatsapp=context.business.owner_whatsapp,
                context=payload,
            )
            self._session.add(record)
        else:
            record.context = payload
            record.owner_whatsapp = context.business.owner_whatsapp
        self._session.commit()

    def get(self, client_id: str) -> ClientContext:
        record = self._session.get(ClientRecord, client_id)
        if record is None:
            raise NotFoundError(f"client {client_id}")
        return ClientContext.model_validate(record.context)

    def find_by_whatsapp(self, number: str) -> ClientContext | None:
        stmt = select(ClientRecord).where(ClientRecord.owner_whatsapp == number)
        record = self._session.scalars(stmt).first()
        return ClientContext.model_validate(record.context) if record else None

    def list_client_ids(self) -> list[str]:
        return list(self._session.scalars(select(ClientRecord.client_id)))


class _ScopedRepository:
    def __init__(self, session: Session, client_id: str):
        self._session = session
        self.client_id = client_id


class ContentQueueRepository(_ScopedRepository):
    def add(self, draft: DraftItem) -> None:
        if draft.client_id != self.client_id:
            raise ValueError("draft.client_id does not match repository scope")
        record = DraftRecord(
            id=draft.id,
            client_id=self.client_id,
            kind=draft.kind.value,
            state=draft.state.value,
            payload=draft.model_dump(mode="json"),
            scheduled_for=draft.scheduled_for,
            expires_at=draft.expires_at,
        )
        self._session.add(record)
        self._session.commit()

    def save(self, draft: DraftItem) -> None:
        record = self._get_record(draft.id)
        record.state = draft.state.value
        record.payload = draft.model_dump(mode="json")
        record.expires_at = draft.expires_at
        self._session.commit()

    def get(self, draft_id: str) -> DraftItem:
        return DraftItem.model_validate(self._get_record(draft_id).payload)

    def find_by_prefix(self, prefix: str) -> DraftItem | None:
        stmt = select(DraftRecord).where(
            DraftRecord.client_id == self.client_id,
            DraftRecord.id.startswith(prefix.lower()),
        )
        record = self._session.scalars(stmt).first()
        return DraftItem.model_validate(record.payload) if record else None

    def list(self, state: ApprovalState | None = None) -> list[DraftItem]:
        stmt = select(DraftRecord).where(DraftRecord.client_id == self.client_id)
        if state is not None:
            stmt = stmt.where(DraftRecord.state == state.value)
        stmt = stmt.order_by(DraftRecord.created_at)
        return [DraftItem.model_validate(r.payload) for r in self._session.scalars(stmt)]

    def _get_record(self, draft_id: str) -> DraftRecord:
        record = self._session.get(DraftRecord, draft_id)
        if record is None or record.client_id != self.client_id:
            raise NotFoundError(f"draft {draft_id}")
        return record


class ApprovalLogRepository(_ScopedRepository):
    def log(self, draft_id: str, from_state: str, to_state: str, actor: str, note: str = "") -> int:
        record = ApprovalLogRecord(
            client_id=self.client_id,
            draft_id=draft_id,
            from_state=from_state,
            to_state=to_state,
            actor=actor,
            note=note,
        )
        self._session.add(record)
        self._session.commit()
        return record.id

    def for_draft(self, draft_id: str) -> list[ApprovalLogRecord]:
        stmt = (
            select(ApprovalLogRecord)
            .where(
                ApprovalLogRecord.client_id == self.client_id,
                ApprovalLogRecord.draft_id == draft_id,
            )
            .order_by(ApprovalLogRecord.at)
        )
        return list(self._session.scalars(stmt))


class PublishLogRepository(_ScopedRepository):
    def record(
        self, draft_id: str, channel: str, external_ref: str, approval_log_id: int
    ) -> PublishLogRecord:
        entry = PublishLogRecord(
            client_id=self.client_id,
            draft_id=draft_id,
            channel=channel,
            external_ref=external_ref,
            approval_log_id=approval_log_id,
        )
        self._session.add(entry)
        self._session.commit()
        return entry

    def for_draft(self, draft_id: str) -> PublishLogRecord | None:
        stmt = select(PublishLogRecord).where(
            PublishLogRecord.client_id == self.client_id,
            PublishLogRecord.draft_id == draft_id,
        )
        return self._session.scalars(stmt).first()

    def count_between(self, since: datetime, until: datetime, kind: str | None = None) -> int:
        stmt = select(func.count(PublishLogRecord.id)).where(
            PublishLogRecord.client_id == self.client_id,
            PublishLogRecord.published_at >= since,
            PublishLogRecord.published_at < until,
        )
        if kind is not None:
            stmt = stmt.join(DraftRecord, DraftRecord.id == PublishLogRecord.draft_id).where(
                DraftRecord.kind == kind
            )
        return int(self._session.scalar(stmt) or 0)


class MetricsRepository(_ScopedRepository):
    def record(self, metric: str, value: float, at: datetime | None = None) -> None:
        entry = MetricRecord(
            client_id=self.client_id,
            metric=metric,
            value=value,
            recorded_at=at or datetime.now(UTC),
        )
        self._session.add(entry)
        self._session.commit()

    def series(self, metric: str, since: datetime, until: datetime) -> list[MetricRecord]:
        stmt = (
            select(MetricRecord)
            .where(
                MetricRecord.client_id == self.client_id,
                MetricRecord.metric == metric,
                MetricRecord.recorded_at >= since,
                MetricRecord.recorded_at < until,
            )
            .order_by(MetricRecord.recorded_at)
        )
        return list(self._session.scalars(stmt))

    def latest(self, metric: str) -> float | None:
        stmt = (
            select(MetricRecord)
            .where(
                MetricRecord.client_id == self.client_id,
                MetricRecord.metric == metric,
            )
            .order_by(MetricRecord.recorded_at.desc())
        )
        record = self._session.scalars(stmt).first()
        return record.value if record else None


class ReviewRepository(_ScopedRepository):
    """Reviews the Reputation Agent has processed for this client."""

    def record(
        self,
        review_id: str,
        rating: int,
        text: str,
        language: str,
        author: str,
        sentiment: str,
        reply_draft_id: str | None = None,
    ) -> None:
        self._session.add(
            ReviewRecord(
                client_id=self.client_id,
                review_id=review_id,
                rating=rating,
                text=text,
                language=language,
                author=author,
                sentiment=sentiment,
                reply_draft_id=reply_draft_id,
            )
        )
        self._session.commit()

    def seen_ids(self) -> set[str]:
        stmt = select(ReviewRecord.review_id).where(ReviewRecord.client_id == self.client_id)
        return set(self._session.scalars(stmt))

    def get(self, review_id: str) -> ReviewRecord:
        stmt = select(ReviewRecord).where(
            ReviewRecord.client_id == self.client_id,
            ReviewRecord.review_id == review_id,
        )
        record = self._session.scalars(stmt).first()
        if record is None:
            raise NotFoundError(f"review {review_id}")
        return record

    def mark_replied(self, review_id: str, reply_draft_id: str) -> None:
        record = self.get(review_id)
        record.reply_draft_id = reply_draft_id
        record.replied_at = datetime.now(UTC)
        self._session.commit()

    def count_between(self, since: datetime, until: datetime) -> int:
        stmt = select(func.count(ReviewRecord.id)).where(
            ReviewRecord.client_id == self.client_id,
            ReviewRecord.seen_at >= since,
            ReviewRecord.seen_at < until,
        )
        return int(self._session.scalar(stmt) or 0)

    def replied_count_between(self, since: datetime, until: datetime) -> int:
        stmt = select(func.count(ReviewRecord.id)).where(
            ReviewRecord.client_id == self.client_id,
            ReviewRecord.replied_at.is_not(None),
            ReviewRecord.replied_at >= since,
            ReviewRecord.replied_at < until,
        )
        return int(self._session.scalar(stmt) or 0)


SERVICE_WINDOW = timedelta(hours=24)


class ConversationRepository(_ScopedRepository):
    """WhatsApp customer conversations: 24h service-window state + broadcast opt-in."""

    def upsert_inbound(self, customer_number: str, customer_name: str = "") -> ConversationRecord:
        """Record an inbound customer message — (re)opens the free service window."""
        record = self.get(customer_number)
        if record is None:
            record = ConversationRecord(
                client_id=self.client_id,
                customer_number=customer_number,
                customer_name=customer_name,
            )
            self._session.add(record)
        record.last_inbound_at = datetime.now(UTC)
        if customer_name:
            record.customer_name = customer_name
        self._session.commit()
        return record

    def get(self, customer_number: str) -> ConversationRecord | None:
        stmt = select(ConversationRecord).where(
            ConversationRecord.client_id == self.client_id,
            ConversationRecord.customer_number == customer_number,
        )
        return self._session.scalars(stmt).first()

    def window_open(self, customer_number: str) -> bool:
        record = self.get(customer_number)
        if record is None:
            return False
        last = record.last_inbound_at
        if last.tzinfo is None:  # SQLite drops tzinfo on round-trip
            last = last.replace(tzinfo=UTC)
        return datetime.now(UTC) - last < SERVICE_WINDOW

    def opt_out(self, customer_number: str) -> None:
        record = self.get(customer_number)
        if record is not None:
            record.opt_in = False
            self._session.commit()

    def opted_in_numbers(self) -> list[str]:
        stmt = (
            select(ConversationRecord.customer_number)
            .where(
                ConversationRecord.client_id == self.client_id,
                ConversationRecord.opt_in.is_(True),
            )
            .order_by(ConversationRecord.created_at)
        )
        return list(self._session.scalars(stmt))


class EnquiryRepository(_ScopedRepository):
    """Audit log of inbound customer messages and how they were handled."""

    def record(self, customer_number: str, text: str, action: str) -> None:
        self._session.add(
            EnquiryRecord(
                client_id=self.client_id,
                customer_number=customer_number,
                text=text,
                action=action,
            )
        )
        self._session.commit()

    def count_between(self, since: datetime, until: datetime) -> int:
        return int(self._session.scalar(self._count_stmt(since, until)) or 0)

    def auto_answered_between(self, since: datetime, until: datetime) -> int:
        stmt = self._count_stmt(since, until).where(EnquiryRecord.action.in_(["faq", "preorder"]))
        return int(self._session.scalar(stmt) or 0)

    def _count_stmt(self, since: datetime, until: datetime):
        return select(func.count(EnquiryRecord.id)).where(
            EnquiryRecord.client_id == self.client_id,
            EnquiryRecord.at >= since,
            EnquiryRecord.at < until,
        )


class CostLedgerRepository(_ScopedRepository):
    def record(self, category: str, amount_inr: float, note: str = "") -> None:
        self._session.add(
            CostLedgerRecord(
                client_id=self.client_id,
                category=category,
                amount_inr=amount_inr,
                note=note,
            )
        )
        self._session.commit()

    def spend_since(self, since: datetime) -> float:
        stmt = select(func.coalesce(func.sum(CostLedgerRecord.amount_inr), 0.0)).where(
            CostLedgerRecord.client_id == self.client_id,
            CostLedgerRecord.at >= since,
        )
        return float(self._session.scalar(stmt) or 0.0)

    def spend_this_month(self) -> float:
        now = datetime.now(UTC)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return self.spend_since(month_start)
