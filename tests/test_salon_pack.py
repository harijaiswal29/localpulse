"""P3 salon pack tests — prove the Vertical Pack contract beyond product retail.

The salon pack (Family 2, appointment services) must drive the same generic
engine the bakery uses: onboarding types offerings from the pack schema,
content grounds captions in services, and the Engagement Agent's booking flow
runs on pack cues/templates with zero salon logic in the engine.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from localpulse.agents.content import ContentTrigger
from localpulse.context.models import ApprovalState, DraftKind, OfferingType
from localpulse.packs.base import load_pack

SALON_ANSWERS: dict[str, str] = {
    "salon_name": "Blush & Bloom Studio",
    "address": "4 Law College Road",
    "city": "Pune",
    "hours": "10am-8pm, closed Tuesday",
    "owner_whatsapp": "+919810001111",
    "phone": "+912025559876",
    "services": "Haircut ₹250, Gold facial ₹800, Hair spa ₹1200, Bridal makeup package ₹5000",
    "tone": "polished, friendly",
    "languages": "English",
    "festival_offers": "bridal packages for wedding season, Diwali glow facials",
}

SALON_OWNER = SALON_ANSWERS["owner_whatsapp"]
CUSTOMER = "+919911223344"


@pytest.fixture
def salon_context(container, session):
    ctx = container.onboarding_agent(session).run("salon-1", "salon", SALON_ANSWERS)
    container.ensure_client_tools(ctx)
    return ctx


def salon_services(container, session):
    return container.services(session, "salon-1")


class TestPackContract:
    def test_salon_pack_exports_all_contract_pieces(self):
        pack = load_pack("salon")
        assert pack.ref == "salon"
        assert pack.family == 2
        assert pack.templates, "pack must ship content templates"
        assert pack.onboarding_questions, "pack must ship an onboarding question set"
        assert OfferingType.SERVICE in pack.offering_schema.allowed_types
        assert pack.offering_schema.requires_appointment
        assert pack.calendar_weights
        assert pack.playbook.cadence, "pack must define its own cadence"
        assert pack.playbook.engagement.faqs, "pack must ship engagement FAQs"
        assert pack.guardrails.banned_terms

    def test_salon_calendar_weights_party_season(self):
        pack = load_pack("salon")
        assert pack.calendar_weights["diwali"] >= 1.5
        assert pack.calendar_weights["navratri"] >= 1.5


class TestOnboarding:
    def test_offerings_typed_as_appointment_services(self, salon_context):
        assert len(salon_context.offerings) == 4
        assert all(o.type == OfferingType.SERVICE for o in salon_context.offerings)
        assert all(o.requires_appointment for o in salon_context.offerings)
        facial = salon_context.offering_by_name("Gold facial")
        assert facial is not None
        assert facial.price_inr == 800

    def test_bakery_offerings_stay_products(self, container, session, pilot_context):
        # the schema-driven typing must not disturb the existing vertical
        assert all(o.type == OfferingType.PRODUCT for o in pilot_context.offerings)
        assert not any(o.requires_appointment for o in pilot_context.offerings)


class TestContent:
    def test_week_of_drafts_grounded_in_services(self, container, session, salon_context):
        services = salon_services(container, session)
        drafts = services.content_agent.run(
            salon_context, ContentTrigger(week_start=date(2026, 7, 27))
        )
        assert len(drafts) == 3  # posts_per_week from the salon playbook
        service_names = [o.name.lower() for o in salon_context.offerings]
        for draft in drafts:
            assert draft.state == ApprovalState.PENDING_APPROVAL
            assert any(name in draft.caption.lower() for name in service_names)


class TestEngagementSalonPlaybook:
    def test_hours_faq_answered_free(self, container, session, salon_context):
        services = salon_services(container, session)
        result = services.engagement_agent.handle_inbound(
            salon_context, CUSTOMER, "Hi, what time do you open tomorrow?"
        )
        assert result.action == "faq"
        assert "10am-8pm" in result.reply
        assert services.cost_guard.spend_this_month() == 0.0

    def test_walkin_question_falls_through_booking_cue_to_faq(
        self, container, session, salon_context
    ):
        # "appointment" is a booking cue, but with no service named the agent must
        # fall through to the walk-in FAQ instead of guessing a booking
        services = salon_services(container, session)
        result = services.engagement_agent.handle_inbound(
            salon_context, CUSTOMER, "Can I walk in without an appointment?"
        )
        assert result.action == "faq"
        assert "booking ahead" in result.reply

    def test_booking_named_service_quotes_and_alerts_owner(self, container, session, salon_context):
        services = salon_services(container, session)
        result = services.engagement_agent.handle_inbound(
            salon_context, CUSTOMER, "I'd like to book a gold facial for Saturday", "Meera"
        )
        assert result.action == "preorder"
        assert "Gold facial" in result.reply
        assert "₹800" in result.reply
        whatsapp = container.registry.get("salon-1", "whatsapp")
        owner_msgs = [m for m in whatsapp.sent if m.to == SALON_OWNER]
        assert any(CUSTOMER in m.body and "gold facial" in m.body.lower() for m in owner_msgs)

    def test_vague_booking_escalates_instead_of_guessing(self, container, session, salon_context):
        services = salon_services(container, session)
        result = services.engagement_agent.handle_inbound(
            salon_context, CUSTOMER, "can I book a hair treatment?"
        )
        assert result.action == "escalated"
        assert "₹" not in result.reply
        pack = load_pack("salon")
        assert result.reply == pack.playbook.engagement.escalation_ack

    def test_weekly_broadcast_draft_carries_stop_footer(self, container, session, salon_context):
        services = salon_services(container, session)
        # a customer messaging in opts them into the pilot broadcast audience
        services.engagement_agent.handle_inbound(salon_context, CUSTOMER, "prices please?")
        draft = services.engagement_agent.draft_weekly_broadcast(salon_context)
        assert draft is not None
        assert draft.kind == DraftKind.WHATSAPP_BROADCAST
        assert draft.state == ApprovalState.PENDING_APPROVAL
        assert "Reply STOP" in draft.caption
        assert CUSTOMER in draft.meta["recipients"]


class TestMultiVerticalIsolation:
    def test_two_packs_answer_from_their_own_context(
        self, container, session, pilot_context, salon_context
    ):
        bakery = container.services(session, "pilot-1")
        salon = salon_services(container, session)

        bakery_reply = bakery.engagement_agent.handle_inbound(
            pilot_context, CUSTOMER, "what are your hours?"
        )
        salon_reply = salon.engagement_agent.handle_inbound(
            salon_context, CUSTOMER, "what are your hours?"
        )
        assert "8am-9pm" in bakery_reply.reply
        assert "10am-8pm" in salon_reply.reply

        # enquiry logs stay client-scoped
        now = datetime.now(UTC)
        window = (now - timedelta(minutes=1), now + timedelta(minutes=1))
        assert bakery.enquiries.count_between(*window) == 1
        assert salon.enquiries.count_between(*window) == 1
