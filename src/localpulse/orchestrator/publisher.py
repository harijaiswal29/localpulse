"""Publish path. Only approved drafts pass; publishes are idempotent and every
one is logged with the approval that authorised it (golden rule #1, spec §11)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from localpulse.context.models import (
    ApprovalState,
    Channel,
    DraftItem,
    DraftKind,
    PublishedAction,
)
from localpulse.context.repositories import (
    BroadcastDeliveryRepository,
    ContentQueueRepository,
    PublishLogRepository,
    ReviewRepository,
)
from localpulse.orchestrator.approval import ApprovalStateMachine
from localpulse.orchestrator.cost_guard import (
    BudgetExceededError,
    CostGuard,
    MessageCategory,
    MessagePurpose,
)
from localpulse.orchestrator.messaging import send_whatsapp
from localpulse.orchestrator.tool_registry import ToolRegistry
from localpulse.tools.retry import PermanentToolError, TransientToolError
from localpulse.tools.whatsapp import OutboundTemplate, WhatsAppTool

if TYPE_CHECKING:
    from localpulse.container import ClientServices

logger = logging.getLogger(__name__)


def publish_ready(services: ClientServices, registry: ToolRegistry) -> list[PublishedAction]:
    """Publish every draft sitting in APPROVED — the delivery leg for drafts the
    owner's standing preference auto-approved (A0) and the retry path for drafts
    an earlier attempt could not finish. Idempotent; a budget block or a failing
    transport leaves the draft approved and retryable, everything else keeps going."""
    actions: list[PublishedAction] = []
    for draft in services.queue.list(state=ApprovalState.APPROVED):
        try:
            actions.append(
                publish_draft(
                    draft_id=draft.id,
                    approval_log_id=services.state_machine.latest_approval_log_id(draft.id),
                    queue=services.queue,
                    publish_log=services.publish_log,
                    state_machine=services.state_machine,
                    registry=registry,
                    cost_guard=services.cost_guard,
                    reviews=services.reviews,
                    deliveries=services.deliveries,
                )
            )
        except BudgetExceededError:
            logger.warning(
                "[publish:%s] draft %s blocked by budget — stays approved for retry",
                draft.client_id,
                draft.short_id,
            )
        except PartialDeliveryError as exc:
            logger.warning(
                "[publish:%s] broadcast %s delivered %d, %d still owed — stays approved, "
                "the next attempt resumes from there",
                draft.client_id,
                draft.short_id,
                exc.delivered,
                exc.remaining,
            )
        except TransientToolError as exc:
            # Retries inside the tool are already exhausted; the cadence tick is
            # the next, slower retry. Nothing was published, so nothing is lost.
            logger.warning(
                "[publish:%s] draft %s failed on a transient tool error (%s) — "
                "stays approved for retry",
                draft.client_id,
                draft.short_id,
                exc,
            )
    return actions


def _approved_template(draft: DraftItem) -> OutboundTemplate | None:
    """The rendered template the owner approved, carried on the draft since drafting.
    Publishing re-sends exactly those words and parameters — it never re-renders."""
    stored = draft.meta.get("template")
    return OutboundTemplate(**stored) if stored else None


class NotApprovedError(Exception):
    def __init__(self, draft: DraftItem):
        super().__init__(
            f"draft {draft.short_id} is {draft.state}, not approved — refusing to publish"
        )


class PartialDeliveryError(Exception):
    """A multi-recipient send stopped partway. What went out is recorded per
    recipient, so the draft stays approved and the next attempt owes only the rest."""

    def __init__(self, draft: DraftItem, delivered: int, remaining: int):
        self.draft_id = draft.id
        self.delivered = delivered
        self.remaining = remaining
        super().__init__(
            f"broadcast {draft.short_id}: delivered {delivered}, {remaining} still owed"
        )


def _publish_broadcast(
    draft: DraftItem,
    recipients: list[str],
    whatsapp: WhatsAppTool,
    cost_guard: CostGuard,
    deliveries: BroadcastDeliveryRepository,
) -> str:
    """Send a broadcast, at most once per recipient.

    The publish log only records a draft as a whole, so a batch that died on
    recipient 7 of 20 was re-sent in full on the next attempt: the first six
    customers got the message twice and the shop paid for it twice. Each send is
    now recorded against its recipient as it settles, so a retry resumes.

    A recipient the platform permanently rejects is settled as failed and the
    batch carries on — partial delivery beats a batch that can never finish
    (graceful degradation, spec §12.1). A transient failure stops the run: if the
    transport is unhealthy, further sends would only burn budget to fail too.
    """
    settled = deliveries.settled(draft.id)
    pending = [number for number in recipients if number not in settled]
    if pending:
        # All-or-nothing on budget — but only over what is actually still owed.
        cost_guard.ensure_affordable(MessageCategory.MARKETING, len(pending))
    template = _approved_template(draft)

    for number in pending:
        try:
            external_ref = send_whatsapp(
                guard=cost_guard,
                tool=whatsapp,
                to=number,
                body=draft.caption,
                purpose=MessagePurpose.MARKETING_BROADCAST,
                within_service_window=False,
                template=template,
            )
        except PermanentToolError as exc:
            logger.error(
                "[publish:%s] broadcast %s: dropping %s — %s",
                draft.client_id,
                draft.short_id,
                number,
                exc,
            )
            deliveries.record_failed(draft.id, number, str(exc))
            continue
        except TransientToolError as exc:
            logger.warning(
                "[publish:%s] broadcast %s: stopping at %s — %s",
                draft.client_id,
                draft.short_id,
                number,
                exc,
            )
            break
        deliveries.record_sent(draft.id, number, external_ref)

    outstanding = [number for number in recipients if number not in deliveries.settled(draft.id)]
    sent = deliveries.sent_count(draft.id)
    if outstanding:
        raise PartialDeliveryError(draft, delivered=sent, remaining=len(outstanding))
    if sent == 0:
        logger.error(
            "[publish:%s] broadcast %s reached nobody — every recipient was rejected",
            draft.client_id,
            draft.short_id,
        )
    return f"wa-broadcast:{sent}/{len(recipients)}"


def publish_draft(
    draft_id: str,
    approval_log_id: int,
    queue: ContentQueueRepository,
    publish_log: PublishLogRepository,
    state_machine: ApprovalStateMachine,
    registry: ToolRegistry,
    cost_guard: CostGuard | None = None,
    reviews: ReviewRepository | None = None,
    deliveries: BroadcastDeliveryRepository | None = None,
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
            template=_approved_template(draft),
        )
        channel = Channel.WHATSAPP
    elif draft.kind == DraftKind.WHATSAPP_BROADCAST:
        if cost_guard is None:
            raise ValueError("publishing a broadcast requires the cost guard")
        if deliveries is None:
            raise ValueError("publishing a broadcast requires the delivery ledger")
        recipients = [str(number) for number in draft.meta.get("recipients", [])]
        if not recipients:
            raise ValueError("broadcast draft has no recipients")
        external_ref = _publish_broadcast(
            draft=draft,
            recipients=recipients,
            whatsapp=registry.get(draft.client_id, "whatsapp"),
            cost_guard=cost_guard,
            deliveries=deliveries,
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
