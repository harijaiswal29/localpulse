"""Google Business Profile tool. API access is gated (spec §7), so P0 ships a
semi-manual implementation: approved posts land in a per-client publish queue the
operator pushes to GBP by hand. The typed interface stays identical when real
API access arrives."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol


@dataclass
class QueuedPost:
    idempotency_key: str
    caption: str
    image_ref: str | None
    queued_at: datetime


@dataclass
class Review:
    review_id: str
    rating: int
    text: str
    language: str
    author: str


@dataclass
class QueuedReply:
    idempotency_key: str
    review_id: str
    reply: str
    queued_at: datetime


class GbpTool(Protocol):
    def post(self, caption: str, image_ref: str | None, idempotency_key: str) -> str: ...
    def list_reviews(self) -> list[Review]: ...
    def reply_review(self, review_id: str, reply: str, idempotency_key: str) -> str: ...
    def fetch_insights(self) -> dict[str, float]: ...


@dataclass
class SemiManualGbpTool:
    """Queues posts for manual publishing; idempotent on the draft id."""

    client_id: str
    queue: list[QueuedPost] = field(default_factory=list)
    reply_queue: list[QueuedReply] = field(default_factory=list)
    # Until real API access, the operator pastes new reviews in here (or a scraper does).
    reviews: list[Review] = field(default_factory=list)

    def post(self, caption: str, image_ref: str | None, idempotency_key: str) -> str:
        for queued in self.queue:
            if queued.idempotency_key == idempotency_key:
                return f"gbp-manual:{idempotency_key[:8]}"
        self.queue.append(
            QueuedPost(
                idempotency_key=idempotency_key,
                caption=caption,
                image_ref=image_ref,
                queued_at=datetime.now(UTC),
            )
        )
        return f"gbp-manual:{idempotency_key[:8]}"

    def list_reviews(self) -> list[Review]:
        return list(self.reviews)

    def reply_review(self, review_id: str, reply: str, idempotency_key: str) -> str:
        for queued in self.reply_queue:
            if queued.idempotency_key == idempotency_key:
                return f"gbp-reply-manual:{idempotency_key[:8]}"
        self.reply_queue.append(
            QueuedReply(
                idempotency_key=idempotency_key,
                review_id=review_id,
                reply=reply,
                queued_at=datetime.now(UTC),
            )
        )
        return f"gbp-reply-manual:{idempotency_key[:8]}"

    def fetch_insights(self) -> dict[str, float]:
        # Deterministic placeholder until real GBP insights are wired in.
        day = datetime.now(UTC).timetuple().tm_yday
        return {
            "profile_views": 20.0 + (day % 15),
            "searches": 8.0 + (day % 7),
            "review_count": 12.0,
            "avg_rating": 4.4,
        }
