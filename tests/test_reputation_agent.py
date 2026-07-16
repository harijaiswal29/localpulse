"""Reputation Agent (P1): review monitoring, A2 escalation, reply publishing,
and the review-solicitation nudge loop — all through the approval queue."""

from datetime import UTC, datetime, timedelta

import pytest

from localpulse.agents.reputation import (
    check_reply_guardrails,
    classify_review,
)
from localpulse.context.models import ApprovalState, Channel, DraftKind
from localpulse.context.repositories import CostLedgerRepository
from localpulse.orchestrator.cost_guard import BudgetExceededError, CostGuard
from localpulse.orchestrator.publisher import NotApprovedError, publish_draft
from localpulse.packs.base import load_pack
from localpulse.tools.gbp import Review

POSITIVE = Review(
    review_id="rev-pos",
    rating=5,
    text="The modak box was outstanding, so fresh!",
    language="mr",
    author="Sneha K.",
)
NEGATIVE = Review(
    review_id="rev-neg",
    rating=2,
    text="Waited 40 minutes and the cake was stale.",
    language="en",
    author="Rahul P.",
)
AMBIGUOUS = Review(
    review_id="rev-amb",
    rating=4,
    text="Lovely cakes but the service was rude today.",
    language="en",
    author="Asha D.",
)


def seed_reviews(container, *reviews: Review) -> None:
    gbp = container.registry.get("pilot-1", "gbp")
    gbp.reviews.extend(reviews)


class TestClassification:
    def test_low_rating_is_negative(self):
        assert classify_review(NEGATIVE) == "negative"

    def test_good_rating_with_complaint_cue_is_ambiguous(self):
        assert classify_review(AMBIGUOUS) == "ambiguous"

    def test_clean_positive(self):
        assert classify_review(POSITIVE) == "positive"


class TestReplyGuardrails:
    def test_banned_term_rejected(self):
        pack = load_pack("bakery")
        assert check_reply_guardrails("Our cakes cure sadness!", pack) is not None

    def test_empty_rejected(self):
        pack = load_pack("bakery")
        assert check_reply_guardrails("   ", pack) == "empty reply"

    def test_too_long_rejected(self):
        pack = load_pack("bakery")
        reason = check_reply_guardrails("x" * (pack.guardrails.max_caption_chars + 1), pack)
        assert reason == "reply too long"

    def test_clean_reply_passes(self):
        pack = load_pack("bakery")
        assert check_reply_guardrails("Thank you so much, see you soon!", pack) is None


class TestCheckReviews:
    def test_new_reviews_become_pending_reply_drafts(self, container, session, pilot_context):
        seed_reviews(container, POSITIVE, NEGATIVE)
        services = container.services(session, "pilot-1")
        drafts = services.reputation_agent.check_reviews(pilot_context)

        assert len(drafts) == 2
        for draft in drafts:
            assert draft.kind == DraftKind.REVIEW_REPLY
            assert draft.state == ApprovalState.PENDING_APPROVAL

    def test_reply_language_follows_the_review(self, container, session, pilot_context):
        seed_reviews(container, POSITIVE)
        services = container.services(session, "pilot-1")
        (draft,) = services.reputation_agent.check_reviews(pilot_context)
        assert draft.language == "mr"

    def test_each_review_processed_once(self, container, session, pilot_context):
        seed_reviews(container, POSITIVE)
        services = container.services(session, "pilot-1")
        assert len(services.reputation_agent.check_reviews(pilot_context)) == 1
        assert services.reputation_agent.check_reviews(pilot_context) == []

    def test_negative_review_escalates(self, container, session, pilot_context):
        seed_reviews(container, NEGATIVE)
        services = container.services(session, "pilot-1")
        (draft,) = services.reputation_agent.check_reviews(pilot_context)

        assert draft.meta["escalated"] is True
        assert draft.meta["sentiment"] == "negative"
        assert "sorry" in draft.caption.lower()

    def test_ambiguous_review_escalates(self, container, session, pilot_context):
        seed_reviews(container, AMBIGUOUS)
        services = container.services(session, "pilot-1")
        (draft,) = services.reputation_agent.check_reviews(pilot_context)
        assert draft.meta["escalated"] is True

    def test_positive_review_not_escalated(self, container, session, pilot_context):
        seed_reviews(container, POSITIVE)
        services = container.services(session, "pilot-1")
        (draft,) = services.reputation_agent.check_reviews(pilot_context)
        assert draft.meta["escalated"] is False
        assert POSITIVE.author in draft.caption

    def test_owner_notification_flags_escalations(self, container, session, pilot_context):
        seed_reviews(container, POSITIVE, NEGATIVE)
        services = container.services(session, "pilot-1")
        services.reputation_agent.check_reviews(pilot_context)

        whatsapp = container.registry.get("pilot-1", "whatsapp")
        body = whatsapp.sent[-1].body
        assert "⚠️" in body
        assert NEGATIVE.text in body
        assert whatsapp.sent[-1].category == "service_reply"  # owner chat is free


class TestReplyPublishing:
    def approve_and_publish(self, container, services, draft):
        _, approval_log_id = services.state_machine.approve(draft.id, actor="owner")
        return publish_draft(
            draft_id=draft.id,
            approval_log_id=approval_log_id,
            queue=services.queue,
            publish_log=services.publish_log,
            state_machine=services.state_machine,
            registry=container.registry,
            cost_guard=services.cost_guard,
            reviews=services.reviews,
        )

    def test_approved_reply_lands_in_gbp_reply_queue(self, container, session, pilot_context):
        seed_reviews(container, POSITIVE)
        services = container.services(session, "pilot-1")
        (draft,) = services.reputation_agent.check_reviews(pilot_context)

        action = self.approve_and_publish(container, services, draft)

        assert action.channel == Channel.GBP
        assert action.external_ref.startswith("gbp-reply-manual:")
        gbp = container.registry.get("pilot-1", "gbp")
        assert len(gbp.reply_queue) == 1
        assert gbp.reply_queue[0].review_id == POSITIVE.review_id
        assert services.reviews.get(POSITIVE.review_id).replied_at is not None

    def test_republish_is_idempotent(self, container, session, pilot_context):
        seed_reviews(container, POSITIVE)
        services = container.services(session, "pilot-1")
        (draft,) = services.reputation_agent.check_reviews(pilot_context)

        first = self.approve_and_publish(container, services, draft)
        second = publish_draft(
            draft_id=draft.id,
            approval_log_id=first.approval_log_id,
            queue=services.queue,
            publish_log=services.publish_log,
            state_machine=services.state_machine,
            registry=container.registry,
            cost_guard=services.cost_guard,
            reviews=services.reviews,
        )
        assert second.external_ref == first.external_ref
        assert len(container.registry.get("pilot-1", "gbp").reply_queue) == 1

    def test_unapproved_reply_never_publishes(self, container, session, pilot_context):
        seed_reviews(container, NEGATIVE)
        services = container.services(session, "pilot-1")
        (draft,) = services.reputation_agent.check_reviews(pilot_context)

        with pytest.raises(NotApprovedError):
            publish_draft(
                draft_id=draft.id,
                approval_log_id=1,
                queue=services.queue,
                publish_log=services.publish_log,
                state_machine=services.state_machine,
                registry=container.registry,
                cost_guard=services.cost_guard,
                reviews=services.reviews,
            )
        assert container.registry.get("pilot-1", "gbp").reply_queue == []


class TestReviewNudge:
    def test_nudge_drafts_and_sends_on_approval(self, container, session, pilot_context):
        services = container.services(session, "pilot-1")
        draft = services.reputation_agent.draft_review_nudge(
            pilot_context, customer_number="+919900112233", customer_name="Priya"
        )
        assert draft.kind == DraftKind.REVIEW_NUDGE
        assert draft.state == ApprovalState.PENDING_APPROVAL
        assert "Priya" in draft.caption

        _, approval_log_id = services.state_machine.approve(draft.id, actor="owner")
        action = publish_draft(
            draft_id=draft.id,
            approval_log_id=approval_log_id,
            queue=services.queue,
            publish_log=services.publish_log,
            state_machine=services.state_machine,
            registry=container.registry,
            cost_guard=services.cost_guard,
            reviews=services.reviews,
        )

        assert action.channel == Channel.WHATSAPP
        whatsapp = container.registry.get("pilot-1", "whatsapp")
        sent = whatsapp.sent[-1]
        assert sent.to == "+919900112233"
        assert sent.category == "utility"  # outside the service window, never marketing
        assert services.cost_guard.spend_this_month() == pytest.approx(0.32)

    def test_nudge_within_service_window_is_free(self, container, session, pilot_context):
        services = container.services(session, "pilot-1")
        draft = services.reputation_agent.draft_review_nudge(
            pilot_context,
            customer_number="+919900112233",
            customer_name="Priya",
            within_service_window=True,
        )
        _, approval_log_id = services.state_machine.approve(draft.id, actor="owner")
        publish_draft(
            draft_id=draft.id,
            approval_log_id=approval_log_id,
            queue=services.queue,
            publish_log=services.publish_log,
            state_machine=services.state_machine,
            registry=container.registry,
            cost_guard=services.cost_guard,
            reviews=services.reviews,
        )
        assert services.cost_guard.spend_this_month() == 0.0

    def test_exhausted_budget_blocks_the_send(self, container, session, pilot_context):
        services = container.services(session, "pilot-1")
        draft = services.reputation_agent.draft_review_nudge(
            pilot_context, customer_number="+919900112233"
        )
        _, approval_log_id = services.state_machine.approve(draft.id, actor="owner")

        broke_guard = CostGuard(CostLedgerRepository(session, "pilot-1"), monthly_budget_inr=0.0)
        with pytest.raises(BudgetExceededError):
            publish_draft(
                draft_id=draft.id,
                approval_log_id=approval_log_id,
                queue=services.queue,
                publish_log=services.publish_log,
                state_machine=services.state_machine,
                registry=container.registry,
                cost_guard=broke_guard,
                reviews=services.reviews,
            )
        # draft stays approved (retryable) and nothing was sent or logged
        assert services.queue.get(draft.id).state == ApprovalState.APPROVED
        assert services.publish_log.for_draft(draft.id) is None
        assert container.registry.get("pilot-1", "whatsapp").sent == []


class TestMonthlyReportResponseRate:
    def test_report_includes_review_response_line(self, container, session, pilot_context):
        seed_reviews(container, POSITIVE, NEGATIVE)
        services = container.services(session, "pilot-1")
        drafts = services.reputation_agent.check_reviews(pilot_context)

        positive = next(d for d in drafts if not d.meta["escalated"])
        _, approval_log_id = services.state_machine.approve(positive.id, actor="owner")
        publish_draft(
            draft_id=positive.id,
            approval_log_id=approval_log_id,
            queue=services.queue,
            publish_log=services.publish_log,
            state_machine=services.state_machine,
            registry=container.registry,
            cost_guard=services.cost_guard,
            reviews=services.reviews,
        )

        now = datetime.now(UTC)
        report = services.insights_agent.monthly_report(pilot_context, now.year, now.month)
        assert "2 new review(s) came in and 1 got a public reply." in report


class TestWhatsAppApprovalFlow:
    def test_owner_approves_reply_over_whatsapp(self, container, session, pilot_context):
        from localpulse.api import main as api_main

        seed_reviews(container, NEGATIVE)
        services = container.services(session, "pilot-1")
        (draft,) = services.reputation_agent.check_reviews(pilot_context)

        reply = api_main._handle_owner_command(services, container, "LIST")
        assert "⚠️" in reply and draft.short_id in reply

        reply = api_main._handle_owner_command(services, container, f"APPROVE {draft.short_id}")
        assert "Approved and published" in reply
        assert container.registry.get("pilot-1", "gbp").reply_queue[0].reply == draft.caption

    def test_expiry_never_auto_publishes_a_reply(self, container, session, pilot_context):
        seed_reviews(container, NEGATIVE)
        services = container.services(session, "pilot-1")
        (draft,) = services.reputation_agent.check_reviews(pilot_context)

        draft.expires_at = datetime.now(UTC) - timedelta(hours=1)
        services.queue.save(draft)
        expired = services.state_machine.sweep_expired()

        assert [d.id for d in expired] == [draft.id]
        assert services.queue.get(draft.id).state == ApprovalState.EXPIRED
        assert container.registry.get("pilot-1", "gbp").reply_queue == []
