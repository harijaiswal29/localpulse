"""P3 WhatsApp message templates + explicit marketing consent.

Outside the 24h service window WhatsApp delivers approved templates only, so every
paid send resolves to one. These tests pin the rules that make that safe:

- the choke point refuses a paid send that has no template (and charges nothing)
- a template's wording is the pack's, and what the owner approved is what is sent
- inside the window the same words go out free-form, so nothing is paid for twice
- marketing needs consent the customer actually gave
"""

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from localpulse.agents.content import ContentTrigger
from localpulse.api import main as api_main
from localpulse.api.main import create_app
from localpulse.container import Container
from localpulse.context.models import (
    ApprovalState,
    DraftKind,
    MessageTemplate,
    TemplateSlot,
)
from localpulse.context.repositories import ClientRepository
from localpulse.orchestrator.cost_guard import (
    CATEGORY_COST_INR,
    MessageCategory,
    MessagePurpose,
)
from localpulse.orchestrator.messaging import send_whatsapp
from localpulse.orchestrator.publisher import publish_draft, publish_ready
from localpulse.orchestrator.templates import (
    SLOT_CATEGORY,
    TemplateRequiredError,
    meta_payload,
    render,
)
from localpulse.packs.base import VerticalPack, load_pack
from localpulse.tools.whatsapp import CloudApiWhatsAppTool
from tests.conftest import (
    PILOT_ANSWERS,
    make_test_settings,
    open_owner_window,
    opt_in_customer,
)

CUSTOMER = "+919900112233"
OWNER = PILOT_ANSWERS["owner_whatsapp"]
WEEK = "2026-07-27"


def whatsapp(container):
    return container.registry.get("pilot-1", "whatsapp")


def sent_to(container, number):
    return [m for m in whatsapp(container).sent if m.to == number]


class TestPackTemplateContract:
    @pytest.mark.parametrize("ref", ["bakery", "salon"])
    def test_every_pack_covers_every_slot(self, ref):
        pack = load_pack(ref)
        assert {t.slot for t in pack.message_templates} == set(TemplateSlot)
        for template in pack.message_templates:
            assert template.name.startswith(ref)  # names are per-business on Meta's side

    def test_marketing_template_must_carry_the_opt_out(self):
        with pytest.raises(ValidationError, match="STOP"):
            MessageTemplate(
                slot=TemplateSlot.WEEKLY_OFFER,
                name="pack_weekly_offer_v1",
                body="This week at {business_name}: {offer}. Come by!",
            )

    @pytest.mark.parametrize(
        "body",
        [
            "{business_name} has news for you today.",  # opens on a placeholder
            "Here is this week's news from {business_name}",  # closes on one
            "News from {business_name} {summary} today.",  # two in a row
            "{summary} and again {summary} today, says the shop.",  # repeated
        ],
    )
    def test_bodies_meta_would_reject_are_rejected_here(self, body):
        with pytest.raises(ValidationError):
            MessageTemplate(slot=TemplateSlot.OWNER_ALERT, name="pack_owner_alert_v1", body=body)

    def test_placeholder_the_engine_cannot_fill_is_rejected(self):
        with pytest.raises(ValidationError, match="cannot fill"):
            MessageTemplate(
                slot=TemplateSlot.REVIEW_NUDGE,
                name="pack_review_nudge_v1",
                body="Hi {customer_name}, how was your {haircut}? Please review us.",
            )

    def test_a_pack_cannot_declare_two_templates_for_one_slot(self):
        bakery = load_pack("bakery")
        with pytest.raises(ValidationError, match="owner_alert"):
            VerticalPack(
                **{
                    **bakery.model_dump(),
                    "message_templates": [
                        bakery.message_template(TemplateSlot.OWNER_ALERT).model_dump(),
                        bakery.message_template(TemplateSlot.OWNER_ALERT).model_dump(),
                    ],
                }
            )


class TestChokePointRule:
    def test_paid_send_without_a_template_is_refused_and_costs_nothing(
        self, container, session, pilot_context
    ):
        services = container.services(session, "pilot-1")
        with pytest.raises(TemplateRequiredError):
            send_whatsapp(
                guard=services.cost_guard,
                tool=whatsapp(container),
                to=CUSTOMER,
                body="a cold marketing message, in free text",
                purpose=MessagePurpose.MARKETING_BROADCAST,
                within_service_window=False,
            )
        assert whatsapp(container).sent == []
        assert services.cost_guard.spend_this_month() == 0.0

    def test_inside_the_window_the_same_words_go_free_form(self, container, session, pilot_context):
        services = container.services(session, "pilot-1")
        rendered = render(
            load_pack("bakery").message_template(TemplateSlot.REVIEW_NUDGE),
            {"customer_name": "Priya", "business_name": "Mane's Bakehouse", "city": "Pune"},
        )
        send_whatsapp(
            guard=services.cost_guard,
            tool=whatsapp(container),
            to=CUSTOMER,
            purpose=MessagePurpose.NOTIFICATION,
            within_service_window=True,
            template=rendered,
        )
        sent = whatsapp(container).sent[-1]
        assert sent.body == rendered.body
        assert sent.template == ""  # free-form: no template fee inside the window
        assert services.cost_guard.spend_this_month() == 0.0


class TestReviewNudge:
    def draft(self, services, ctx, **kwargs):
        return services.reputation_agent.draft_review_nudge(
            ctx, customer_number=CUSTOMER, customer_name="Priya", **kwargs
        )

    def test_wording_is_the_pack_template_not_model_output(self, container, session, pilot_context):
        services = container.services(session, "pilot-1")
        template = load_pack("bakery").message_template(TemplateSlot.REVIEW_NUDGE)
        expected = render(
            template,
            {
                "customer_name": "Priya",
                "business_name": pilot_context.business.name,
                "city": "Pune",
            },
        )
        first = self.draft(services, pilot_context)
        second = self.draft(services, pilot_context)
        assert first.caption == second.caption == expected.body  # deterministic, every time
        assert first.meta["template"]["name"] == template.name

    def test_publish_sends_the_approved_template_at_the_utility_rate(
        self, container, session, pilot_context
    ):
        services = container.services(session, "pilot-1")
        draft = self.draft(services, pilot_context)
        approved, log_id = services.state_machine.approve(draft.id, actor="owner")
        publish_draft(
            draft_id=approved.id,
            approval_log_id=log_id,
            queue=services.queue,
            publish_log=services.publish_log,
            state_machine=services.state_machine,
            registry=container.registry,
            cost_guard=services.cost_guard,
        )
        sent = sent_to(container, CUSTOMER)[-1]
        assert sent.template == "bakery_review_nudge_v1"
        assert sent.category == MessageCategory.UTILITY.value
        assert sent.body == draft.caption  # approved text == delivered text
        assert services.cost_guard.spend_this_month() == CATEGORY_COST_INR[MessageCategory.UTILITY]

    def test_an_owner_edit_is_refused_because_the_wording_is_approved(
        self, container, session, pilot_context
    ):
        services = container.services(session, "pilot-1")
        clients = ClientRepository(session)
        draft = self.draft(services, pilot_context)
        reply = api_main._handle_owner_command(
            services, container, clients, f"EDIT {draft.short_id} thanks come again"
        )
        assert "wording is fixed" in reply
        assert services.queue.get(draft.id).caption == draft.caption


class TestBroadcast:
    def draft(self, container, session, ctx):
        services = container.services(session, "pilot-1")
        opt_in_customer(services, ctx, CUSTOMER)
        opt_in_customer(services, ctx, "+919900445566", "Arjun")
        return services, services.engagement_agent.draft_weekly_broadcast(ctx)

    def test_every_recipient_gets_exactly_what_the_owner_approved(
        self, container, session, pilot_context
    ):
        services, draft = self.draft(container, session, pilot_context)
        approved, log_id = services.state_machine.approve(draft.id, actor="owner")
        publish_draft(
            draft_id=approved.id,
            approval_log_id=log_id,
            queue=services.queue,
            publish_log=services.publish_log,
            state_machine=services.state_machine,
            registry=container.registry,
            cost_guard=services.cost_guard,
            deliveries=services.deliveries,
        )
        marketing = [m for m in whatsapp(container).sent if m.category == "marketing"]
        assert {m.to for m in marketing} == {CUSTOMER, "+919900445566"}
        assert {m.template for m in marketing} == {"bakery_weekly_offer_v1"}
        assert {m.body for m in marketing} == {draft.caption}  # no per-recipient drift

    def test_the_offer_line_is_the_only_generated_part(self, container, session, pilot_context):
        _, draft = self.draft(container, session, pilot_context)
        template = load_pack("bakery").message_template(TemplateSlot.WEEKLY_OFFER)
        fixed_head = template.body.split("{offer}")[0].format(
            business_name=pilot_context.business.name
        )
        assert draft.caption.startswith(fixed_head)
        assert draft.caption.endswith("Reply STOP to opt out of offers.")
        assert draft.meta["template"]["params"][1] not in fixed_head  # the offer param

    def test_owner_edit_rewrites_the_offer_inside_the_approved_wording(
        self, container, session, pilot_context
    ):
        services, draft = self.draft(container, session, pilot_context)
        reply = api_main._handle_owner_command(
            services,
            container,
            ClientRepository(session),
            f"EDIT {draft.short_id} modak boxes at ₹280 for Ganesh Chaturthi",
        )
        assert "now reads" in reply
        edited = services.queue.get(draft.id)
        assert "modak boxes at ₹280" in edited.caption
        assert edited.caption.endswith("Reply STOP to opt out of offers.")  # fixed part survives
        # the edit reaches the wire, not just the preview
        assert edited.meta["template"]["params"][1] == "modak boxes at ₹280 for Ganesh Chaturthi"
        assert edited.meta["template"]["body"] == edited.caption


class TestOwnerAlerts:
    def test_cold_window_owner_gets_the_short_template_that_reopens_it(
        self, container, session, pilot_context
    ):
        services = container.services(session, "pilot-1")
        services.content_agent.run(pilot_context, ContentTrigger(week_start="2026-07-27"))
        alert = sent_to(container, OWNER)[-1]
        assert alert.template == "bakery_owner_alert_v1"
        assert alert.category == MessageCategory.UTILITY.value
        assert "Reply LIST" in alert.body
        assert "\n" not in alert.body  # a template parameter cannot carry newlines

    def test_open_window_owner_gets_the_full_digest_free(self, container, session, pilot_context):
        services = container.services(session, "pilot-1")
        open_owner_window(services)
        services.content_agent.run(pilot_context, ContentTrigger(week_start="2026-07-27"))
        digest = sent_to(container, OWNER)[-1]
        assert digest.template == ""
        assert "Reply APPROVE" in digest.body
        assert services.cost_guard.spend_this_month() == 0.0


class TestColdWindowRecovery:
    def test_report_command_fetches_what_the_alert_could_only_nudge_about(
        self, container, session, pilot_context
    ):
        services = container.services(session, "pilot-1")
        services.insights_agent.collect_daily(pilot_context)
        reply = api_main._handle_owner_command(
            services, container, ClientRepository(session), "REPORT"
        )
        assert "your month in review" in reply
        assert services.cost_guard.spend_this_month() == 0.0  # answered in-window, free


class TestExplicitConsent:
    def test_messaging_the_shop_is_not_consent(self, container, session, pilot_context):
        services = container.services(session, "pilot-1")
        services.engagement_agent.handle_inbound(pilot_context, CUSTOMER, "what are your hours?")
        assert services.conversations.opted_in_numbers() == []
        assert services.engagement_agent.draft_weekly_broadcast(pilot_context) is None

    def test_start_records_consent_with_its_basis(self, container, session, pilot_context):
        services = container.services(session, "pilot-1")
        result = services.engagement_agent.handle_inbound(pilot_context, CUSTOMER, "START")
        assert result.action == "opt_in"
        record = services.conversations.get(CUSTOMER)
        assert record.opt_in is True
        assert record.opt_in_source == "keyword"
        assert record.opt_in_at is not None

    def test_stop_after_start_revokes_and_is_recorded(self, container, session, pilot_context):
        services = container.services(session, "pilot-1")
        opt_in_customer(services, pilot_context, CUSTOMER)
        services.engagement_agent.handle_inbound(pilot_context, CUSTOMER, "STOP")
        record = services.conversations.get(CUSTOMER)
        assert record.opt_in is False
        assert record.opt_in_source == "revoked"

    def test_pilot_implied_mode_opts_in_on_first_contact_only(self):
        settings = make_test_settings()
        settings.marketing_opt_in_mode = "implied"
        container = Container(settings)
        with container.session() as session:
            ctx = container.onboarding_agent(session).run("pilot-1", "bakery", PILOT_ANSWERS)
            container.ensure_client_tools(ctx)
            services = container.services(session, "pilot-1")
            agent = services.engagement_agent
            agent.handle_inbound(ctx, CUSTOMER, "what are your hours?")
            assert services.conversations.get(CUSTOMER).opt_in_source == "implied_inbound"
            agent.handle_inbound(ctx, CUSTOMER, "STOP")
            agent.handle_inbound(ctx, CUSTOMER, "where are you?")
            # a later message must not revive consent the customer took away
            assert services.conversations.opted_in_numbers() == []


class TestCloudApiTransport:
    def fake_post(self, monkeypatch, captured):
        def post(url, headers=None, json=None, timeout=None):
            captured.update(url=url, json=json)
            return httpx.Response(200, json={"messages": [{"id": "wamid.TEST123"}]})

        monkeypatch.setattr("localpulse.tools.whatsapp.httpx.post", post)

    def test_template_send_uses_the_template_payload(self, monkeypatch):
        captured: dict = {}
        self.fake_post(monkeypatch, captured)
        rendered = render(
            load_pack("bakery").message_template(TemplateSlot.WEEKLY_OFFER),
            {"business_name": "Mane's Bakehouse", "offer": "modak boxes ₹300"},
        )
        tool = CloudApiWhatsAppTool(client_id="pilot-1", api_key="k", phone_number_id="555")
        assert tool.send(CUSTOMER, rendered.body, "marketing", rendered) == "wamid.TEST123"
        payload = captured["json"]
        assert payload["type"] == "template"
        assert payload["template"]["name"] == "bakery_weekly_offer_v1"
        assert payload["template"]["language"] == {"code": "en"}
        assert [p["text"] for p in payload["template"]["components"][0]["parameters"]] == [
            "Mane's Bakehouse",
            "modak boxes ₹300",
        ]

    def test_free_form_send_is_unchanged(self, monkeypatch):
        captured: dict = {}
        self.fake_post(monkeypatch, captured)
        tool = CloudApiWhatsAppTool(client_id="pilot-1", api_key="k", phone_number_id="555")
        tool.send(CUSTOMER, "we're open till 9pm", "service_reply")
        assert captured["json"]["type"] == "text"
        assert captured["json"]["text"]["body"] == "we're open till 9pm"


class TestSubmissionPayload:
    @pytest.mark.parametrize("ref", ["bakery", "salon"])
    def test_payload_declares_the_category_the_cost_guard_charges(self, ref):
        for template in load_pack(ref).message_templates:
            payload = meta_payload(template, {})
            assert payload["category"] == SLOT_CATEGORY[template.slot].value.upper()
            body = payload["components"][0]["text"]
            assert "{{1}}" in body or not template.params
            assert "{" + (template.params[0] if template.params else "x") + "}" not in body
            examples = payload["components"][0].get("example", {}).get("body_text", [[]])[0]
            assert len(examples) == len(template.params)


class TestApiOptIn:
    def test_broadcast_needs_consent_collected_over_the_api_too(self):
        client = TestClient(create_app(make_test_settings()))
        client.post(
            "/clients/pilot-1/onboard", json={"pack_ref": "bakery", "answers": PILOT_ANSWERS}
        )
        inbound = {"customer_number": CUSTOMER, "text": "menu please"}
        client.post("/clients/pilot-1/engagement/inbound", json=inbound)
        assert client.post("/clients/pilot-1/engagement/broadcast", json={}).status_code == 422
        client.post(
            "/clients/pilot-1/engagement/inbound",
            json={"customer_number": CUSTOMER, "text": "START"},
        )
        response = client.post("/clients/pilot-1/engagement/broadcast", json={})
        assert response.status_code == 200
        assert response.json()["meta"]["template"]["name"] == "bakery_weekly_offer_v1"


class TestWorkerDelivery:
    def test_auto_published_broadcast_goes_out_as_a_template(
        self, container, session, pilot_context
    ):
        services = container.services(session, "pilot-1")
        opt_in_customer(services, pilot_context, CUSTOMER)
        pilot_context.approval_prefs.auto_publish_kinds = [DraftKind.WHATSAPP_BROADCAST]
        ClientRepository(session).save(pilot_context)
        services = container.services(session, "pilot-1")
        draft = services.engagement_agent.draft_weekly_broadcast(pilot_context)
        assert draft.state == ApprovalState.APPROVED
        (action,) = publish_ready(services, container.registry)
        assert action.external_ref == "wa-broadcast:1/1"
        assert sent_to(container, CUSTOMER)[-1].template == "bakery_weekly_offer_v1"
