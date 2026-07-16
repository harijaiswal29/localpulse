"""Publish path. Only approved drafts pass; publishes are idempotent and every
one is logged with the approval that authorised it (golden rule #1, spec §11)."""

from __future__ import annotations

from localpulse.context.models import (
    ApprovalState,
    Channel,
    DraftItem,
    DraftKind,
    PublishedAction,
)
from localpulse.context.repositories import (
    ContentQueueRepository,
    PublishLogRepository,
    ReviewRepository,
)
from localpulse.orchestrator.approval import ApprovalStateMachine
from localpulse.orchestrator.cost_guard import CostGuard, MessagePurpose
from localpulse.orchestrator.messaging import send_whatsapp
from localpulse.orchestrator.tool_registry import ToolRegistry


class NotApprovedError(Exception):
    def __init__(self, draft: DraftItem):
        super().__init__(
            f"draft {draft.short_id} is {draft.state}, not approved — refusing to publish"
        )


def publish_draft(
    draft_id: str,
    approval_log_id: int,
    queue: ContentQueueRepository,
    publish_log: PublishLogRepository,
    state_machine: ApprovalStateMachine,
    registry: ToolRegistry,
    cost_guard: CostGuard | None = None,
    reviews: ReviewRepository | None = None,
) -> PublishedAction:
    draft = queue.get(draft_id)

    existing = publish_log.for_draft(draft.id)
    if existing is not None:  # idempotent: a retried publish returns the original action
        return PublishedAction(
            draft_id=draft.id,
            client_id=draft.client_id,
            channel=Channel(existing.channel),
            external_ref=existing.external_ref,
            approval_log_id=existing.approval_log_id,
            published_at=existing.published_at,
        )

    if draft.state != ApprovalState.APPROVED:
        raise NotApprovedError(draft)  # fail closed on anything public

    if draft.kind == DraftKind.REVIEW_REPLY:
        gbp = registry.get(draft.client_id, "gbp")
        external_ref = gbp.reply_review(
            review_id=draft.meta["review_id"], reply=draft.caption, idempotency_key=draft.id
        )
        channel = Channel.GBP
        if reviews is not None:
            reviews.mark_replied(draft.meta["review_id"], draft.id)
    elif draft.kind == DraftKind.REVIEW_NUDGE:
        if cost_guard is None:
            raise ValueError("publishing a review nudge requires the cost guard")
        # Budget check happens inside send_whatsapp — a blocked send leaves the
        # draft approved-but-unpublished, safe to retry.
        external_ref = send_whatsapp(
            guard=cost_guard,
            tool=registry.get(draft.client_id, "whatsapp"),
            to=draft.meta["customer_number"],
            body=draft.caption,
            purpose=MessagePurpose.NOTIFICATION,
            within_service_window=bool(draft.meta.get("within_service_window", False)),
        )
        channel = Channel.WHATSAPP
    else:
        gbp = registry.get(draft.client_id, "gbp")
        external_ref = gbp.post(
            caption=draft.caption, image_ref=draft.image_ref, idempotency_key=draft.id
        )
        channel = Channel.GBP

    state_machine.mark_published(draft.id, note=f"published as {external_ref}")
    entry = publish_log.record(
        draft_id=draft.id,
        channel=channel.value,
        external_ref=external_ref,
        approval_log_id=approval_log_id,
    )
    return PublishedAction(
        draft_id=draft.id,
        client_id=draft.client_id,
        channel=channel,
        external_ref=external_ref,
        approval_log_id=approval_log_id,
        published_at=entry.published_at,
    )
