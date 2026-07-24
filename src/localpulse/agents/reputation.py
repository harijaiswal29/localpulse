"""Reputation Agent (A1, A2 for negatives) — watches for new reviews, drafts
responses in the review's own language and the owner's voice, and runs the
review-solicitation nudge loop (spec §5.3). Negative or ambiguous reviews are
escalated to the owner and never auto-send; every reply enters the approval queue."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

from localpulse.agents.common import check_text_guardrails
from localpulse.context.models import (
    ApprovalState,
    ClientContext,
    DraftItem,
    DraftKind,
    TemplateSlot,
)
from localpulse.context.repositories import ConversationRepository, ReviewRepository
from localpulse.llm.gateway import ModelGateway
from localpulse.orchestrator.approval import ApprovalStateMachine
from localpulse.orchestrator.cost_guard import CostGuard, MessagePurpose
from localpulse.orchestrator.messaging import notify_owner
from localpulse.orchestrator.templates import TemplateRenderError, render
from localpulse.orchestrator.tool_registry import ToolRegistry
from localpulse.packs.base import VerticalPack, load_pack
from localpulse.tools.gbp import Review

logger = logging.getLogger(__name__)

# Generic complaint cues (not vertical-specific): a 4-5★ review containing one of
# these is "ambiguous" and escalates (A2) rather than getting a cheery thank-you.
NEGATIVE_CUES = [
    "disappoint",
    "stale",
    "rude",
    "worst",
    "refund",
    "never again",
    "too slow",
    "unhygienic",
    "dirty",
    "overpriced",
    "cold and",
    "not fresh",
]


@dataclass
class ProcessedReview:
    review: Review
    sentiment: str  # positive | negative | ambiguous
    draft: DraftItem | None  # None if generation failed guardrails twice


def classify_review(review: Review) -> str:
    """A2 triage: low rating -> negative; good rating with complaint cues -> ambiguous."""
    if review.rating <= 3:
        return "negative"
    lowered = review.text.lower()
    if any(cue in lowered for cue in NEGATIVE_CUES):
        return "ambiguous"
    return "positive"


def check_reply_guardrails(reply: str, pack: VerticalPack) -> str | None:
    """Return a rejection reason, or None if the reply is safe to show the owner."""
    return check_text_guardrails(reply, pack)


class ReputationAgent:
    task_profile = "reputation"

    def __init__(
        self,
        gateway: ModelGateway,
        registry: ToolRegistry,
        state_machine: ApprovalStateMachine,
        cost_guard: CostGuard,
        reviews: ReviewRepository,
        conversations: ConversationRepository,
    ):
        self._gateway = gateway
        self._registry = registry
        self._state_machine = state_machine
        self._cost_guard = cost_guard
        self._reviews = reviews
        self._conversations = conversations

    def check_reviews(self, ctx: ClientContext) -> list[DraftItem]:
        """Pull reviews from GBP, draft a reply for each unseen one, notify the owner."""
        pack = load_pack(ctx.vertical_pack_ref)
        gbp = self._registry.get(ctx.client_id, "gbp")
        seen = self._reviews.seen_ids()

        processed: list[ProcessedReview] = []
        for review in gbp.list_reviews():
            if review.review_id in seen:
                continue
            sentiment = classify_review(review)
            draft = self._draft_reply(ctx, pack, review, sentiment)
            self._reviews.record(
                review_id=review.review_id,
                rating=review.rating,
                text=review.text,
                language=review.language,
                author=review.author,
                sentiment=sentiment,
                reply_draft_id=draft.id if draft else None,
            )
            processed.append(ProcessedReview(review, sentiment, draft))

        if processed:
            self._notify_owner(ctx, processed)
        return [p.draft for p in processed if p.draft is not None]

    def draft_review_nudge(
        self,
        ctx: ClientContext,
        customer_number: str,
        customer_name: str = "",
        within_service_window: bool = False,
    ) -> DraftItem | None:
        """Draft a post-purchase 'leave us a review' WhatsApp nudge (A1 — owner
        approves before anything is sent; the Cost Guard prices the send).

        A nudge normally lands after the 24h window has closed, where WhatsApp
        delivers approved templates only — so the wording is the pack's utility
        template, not model output. Nothing is generated here: the same message
        every time is exactly what Meta approved.
        """
        pack = load_pack(ctx.vertical_pack_ref)
        template = pack.message_template(TemplateSlot.REVIEW_NUDGE)
        if template is None:
            logger.warning(
                "[reputation:%s] pack %r has no review-nudge template — cannot nudge",
                ctx.client_id,
                pack.ref,
            )
            return None
        try:
            rendered = render(
                template,
                {
                    "business_name": ctx.business.name,
                    "city": ctx.business.city,
                    "customer_name": customer_name or "there",
                },
            )
        except TemplateRenderError as exc:
            logger.warning("[reputation:%s] nudge template not filled: %s", ctx.client_id, exc)
            return None
        reason = check_text_guardrails(rendered.body, pack)
        if reason is not None:
            logger.warning("[reputation:%s] nudge template rejected: %s", ctx.client_id, reason)
            return None
        draft = DraftItem(
            client_id=ctx.client_id,
            kind=DraftKind.REVIEW_NUDGE,
            caption=rendered.body,
            language=ctx.brand_voice.languages[0],
            time_sensitive=False,
            state=ApprovalState.DRAFTED,
            meta={
                "customer_number": customer_number,
                "customer_name": customer_name,
                "within_service_window": within_service_window,
                "template_slot": TemplateSlot.REVIEW_NUDGE.value,
                "template": asdict(rendered),  # approved wording travels with the draft
            },
        )
        return self._state_machine.submit(draft, actor="reputation_agent")

    def _draft_reply(
        self, ctx: ClientContext, pack: VerticalPack, review: Review, sentiment: str
    ) -> DraftItem | None:
        system = (
            f"You write public replies to Google reviews for {ctx.business.name}, "
            f"a {ctx.business.category.lower()} in {ctx.business.city}. "
            f"Tone: {', '.join(ctx.brand_voice.tone) or 'warm'}. "
            "Reply in the same language as the review. Never promise refunds or "
            "compensation, never argue, and never make health claims."
        )
        prompt = "\n".join(
            [
                f"business: {ctx.business.name}",
                f"reviewer: {review.author}",
                f"rating: {review.rating}",
                f"language: {review.language}",
                f"sentiment: {sentiment}",
                f"review: {review.text}",
                f"style: {pack.playbook.review_reply_style}",
                "Write the public reply. For negative reviews: acknowledge, apologise "
                "once, and move the conversation to WhatsApp — do not get defensive.",
            ]
        )
        body = self._complete_with_guardrails(ctx, pack, prompt, system)
        if body is None:
            logger.warning(
                "[reputation:%s] could not draft a safe reply to review %s — "
                "owner will be asked to reply manually",
                ctx.client_id,
                review.review_id,
            )
            return None
        draft = DraftItem(
            client_id=ctx.client_id,
            kind=DraftKind.REVIEW_REPLY,
            caption=body,
            language=review.language,
            time_sensitive=False,
            state=ApprovalState.DRAFTED,
            meta={
                "review_id": review.review_id,
                "author": review.author,
                "rating": review.rating,
                "review_text": review.text,
                "sentiment": sentiment,
                # A2: negatives/ambiguous are flagged for the owner's personal attention
                # and must never be auto-sent, whatever future approval prefs allow.
                "escalated": sentiment != "positive",
            },
        )
        return self._state_machine.submit(draft, actor="reputation_agent")

    def _complete_with_guardrails(
        self, ctx: ClientContext, pack: VerticalPack, prompt: str, system: str
    ) -> str | None:
        for attempt in range(2):
            body = self._gateway.complete(self.task_profile, prompt, system=system).strip()
            reason = check_reply_guardrails(body, pack)
            if reason is None:
                return body
            logger.info(
                "[reputation:%s] draft rejected (%s), attempt %d",
                ctx.client_id,
                reason,
                attempt + 1,
            )
            prompt += f"\nThe previous draft was rejected because: {reason}. Fix that."
        return None

    def _notify_owner(self, ctx: ClientContext, processed: list[ProcessedReview]) -> None:
        if not self._registry.is_connected(ctx.client_id, "whatsapp"):
            return
        lines = [f"⭐ {len(processed)} new review(s) on your Google profile:"]
        for item in processed:
            review = item.review
            stars = "★" * review.rating + "☆" * (5 - review.rating)
            lines.append(f'\n{stars} {review.author}: "{review.text}"')
            if item.sentiment != "positive":
                lines.append("⚠️ This one needs your personal attention.")
            if item.draft is not None:
                lines.append(f"Suggested reply [{item.draft.short_id}]: {item.draft.caption}")
            else:
                lines.append("I couldn't draft a safe reply — please respond to this one yourself.")
        lines.append("\nReply APPROVE <id>, EDIT <id> <new text>, or SKIP <id> for each.")
        notify_owner(
            guard=self._cost_guard,
            tool=self._registry.get(ctx.client_id, "whatsapp"),
            ctx=ctx,
            pack=load_pack(ctx.vertical_pack_ref),
            body="\n".join(lines),
            purpose=MessagePurpose.APPROVAL_REQUEST,
            window_open=self._conversations.window_open(ctx.business.owner_whatsapp),
            summary=f"{len(processed)} new review(s) to look at",
        )
