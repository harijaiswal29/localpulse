"""P2 Engagement Agent tests, mapped to the Definition of Done:

- FAQs and simple pre-orders auto-answered free inside the service window (A0)
- anything not confidently matched escalates to the owner — never a guessed reply (A2)
- weekly offer broadcasts are A1 drafts, priced as marketing by the Cost Guard
- the Cloud API BSP adapter sits behind the WhatsAppTool interface; mock stays default
- all FAQ/pre-order behaviour comes from the pack playbook
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from localpulse.api.main import create_app
from localpulse.container import Container
from localpulse.context.models import ApprovalState, DraftKind
from localpulse.context.repositories import CostLedgerRepository
from localpulse.orchestrator.cost_guard import BudgetExceededError, CostGuard
from localpulse.orchestrator.publisher import publish_draft
from localpulse.tools.whatsapp import CloudApiWhatsAppTool, MockWhatsAppTool
from tests.conftest import PILOT_ANSWERS, make_test_settings

OWNER = PILOT_ANSWERS["owner_whatsapp"]
CUSTOMER = "+919900112233"


def services_for(container, session):
    return container.services(session, "pilot-1")


def whatsapp_tool(container):
    return container.registry.get("pilot-1", "whatsapp")


class TestFaqAutoAnswer:
    def test_hours_question_answered_free(self, container, session, pilot_context):
        services = services_for(container, session)
        result = services.engagement_agent.handle_inbound(
            pilot_context, CUSTOMER, "Hi! What time are you open till today?"
        )
        assert result.action == "faq"
        assert "8am-9pm" in result.reply
        sent = whatsapp_tool(container).sent[-1]
        assert sent.to == CUSTOMER
        assert sent.category == "service_reply"  # A0: free inside the 24h window
        assert services.cost_guard.spend_this_month() == 0.0

    def test_location_question_uses_context_address(self, container, session, pilot_context):
        services = services_for(container, session)
        result = services.engagement_agent.handle_inbound(
            pilot_context, CUSTOMER, "where exactly is your shop?"
        )
        assert result.action == "faq"
        assert "12 FC Road" in result.reply
        assert "Pune" in result.reply

    def test_menu_question_grounded_in_offerings(self, container, session, pilot_context):
        services = services_for(container, session)
        result = services.engagement_agent.handle_inbound(
            pilot_context, CUSTOMER, "Can you share your menu and prices?"
        )
        assert result.action == "faq"
        assert "Modak box ₹300" in result.reply

    def test_enquiry_recorded_for_the_monthly_report(self, container, session, pilot_context):
        services = services_for(container, session)
        services.engagement_agent.handle_inbound(pilot_context, CUSTOMER, "what are your hours?")
        now = datetime.now(UTC)
        window = (now - timedelta(minutes=1), now + timedelta(minutes=1))
        assert services.enquiries.count_between(*window) == 1
        assert services.enquiries.auto_answered_between(*window) == 1


class TestPreorder:
    def test_named_offering_gets_grounded_ack_and_owner_heads_up(
        self, container, session, pilot_context
    ):
        services = services_for(container, session)
        result = services.engagement_agent.handle_inbound(
            pilot_context, CUSTOMER, "I want to order a modak box for Saturday", "Priya"
        )
        assert result.action == "preorder"
        assert "Modak box" in result.reply
        assert "₹300" in result.reply
        # owner got the heads-up to confirm the order
        owner_msgs = [m for m in whatsapp_tool(container).sent if m.to == OWNER]
        assert any(CUSTOMER in m.body and "modak box" in m.body.lower() for m in owner_msgs)

    def test_partial_name_still_confident(self, container, session, pilot_context):
        services = services_for(container, session)
        result = services.engagement_agent.handle_inbound(
            pilot_context, CUSTOMER, "can I book a truffle for tomorrow?"
        )
        assert result.action == "preorder"
        assert "₹550" in result.reply

    def test_vague_product_word_escalates_instead_of_guessing(
        self, container, session, pilot_context
    ):
        # "cake" is in the pack's vague_terms: quoting the truffle cake would be a guess
        services = services_for(container, session)
        result = services.engagement_agent.handle_inbound(
            pilot_context, CUSTOMER, "I need a cake for a birthday party"
        )
        assert result.action == "escalated"
        assert "₹" not in result.reply  # no invented quote


class TestEscalation:
    def test_unknown_question_never_gets_a_guessed_reply(self, container, session, pilot_context):
        services = services_for(container, session)
        result = services.engagement_agent.handle_inbound(
            pilot_context, CUSTOMER, "Do you make sugar-free vegan black forest pastries?"
        )
        assert result.action == "escalated"
        # the customer reply is exactly the pack's holding message — deterministic
        assert result.reply == (
            "Thanks for your message! Let me check with the owner — "
            "you'll hear back right here shortly."
        )
        owner_msgs = [m for m in whatsapp_tool(container).sent if m.to == OWNER]
        assert any("sugar-free vegan black forest" in m.body for m in owner_msgs)

    def test_escalation_reply_is_still_free(self, container, session, pilot_context):
        services = services_for(container, session)
        services.engagement_agent.handle_inbound(pilot_context, CUSTOMER, "random question?!")
        assert services.cost_guard.spend_this_month() == 0.0


class TestOptOut:
    def test_stop_removes_customer_from_broadcast_audience(self, container, session, pilot_context):
        services = services_for(container, session)
        services.engagement_agent.handle_inbound(pilot_context, CUSTOMER, "what are your hours?")
        assert CUSTOMER in services.conversations.opted_in_numbers()
        result = services.engagement_agent.handle_inbound(pilot_context, CUSTOMER, "STOP")
        assert result.action == "opt_out"
        assert CUSTOMER not in services.conversations.opted_in_numbers()

    def test_service_window_tracked_from_inbound(self, container, session, pilot_context):
        services = services_for(container, session)
        assert not services.conversations.window_open(CUSTOMER)
        services.engagement_agent.handle_inbound(pilot_context, CUSTOMER, "hello, timing?")
        assert services.conversations.window_open(CUSTOMER)


class TestWeeklyBroadcast:
    def seed_audience(self, services, ctx, agent):
        agent.handle_inbound(ctx, CUSTOMER, "what are your hours?")
        agent.handle_inbound(ctx, "+919900445566", "menu please", "Arjun")

    def test_draft_enters_approval_queue_with_audience(self, container, session, pilot_context):
        services = services_for(container, session)
        self.seed_audience(services, pilot_context, services.engagement_agent)
        draft = services.engagement_agent.draft_weekly_broadcast(pilot_context)
        assert draft is not None
        assert draft.kind == DraftKind.WHATSAPP_BROADCAST
        assert draft.state == ApprovalState.PENDING_APPROVAL  # A1 — owner must approve
        assert set(draft.meta["recipients"]) == {CUSTOMER, "+919900445566"}
        assert "Reply STOP to opt out" in draft.caption  # engine compliance footer

    def test_no_audience_means_no_draft(self, container, session, pilot_context):
        services = services_for(container, session)
        assert services.engagement_agent.draft_weekly_broadcast(pilot_context) is None

    def test_banned_term_in_offer_fails_guardrails(self, container, session, pilot_context):
        services = services_for(container, session)
        self.seed_audience(services, pilot_context, services.engagement_agent)
        draft = services.engagement_agent.draft_weekly_broadcast(
            pilot_context, offer_text="guaranteed weight loss bread ₹90"
        )
        assert draft is None

    def test_publish_charges_marketing_rate_per_recipient(self, container, session, pilot_context):
        services = services_for(container, session)
        self.seed_audience(services, pilot_context, services.engagement_agent)
        draft = services.engagement_agent.draft_weekly_broadcast(pilot_context)
        approved, approval_log_id = services.state_machine.approve(draft.id, actor="owner")
        action = publish_draft(
            draft_id=approved.id,
            approval_log_id=approval_log_id,
            queue=services.queue,
            publish_log=services.publish_log,
            state_machine=services.state_machine,
            registry=container.registry,
            cost_guard=services.cost_guard,
        )
        assert action.external_ref == "wa-broadcast:2"
        marketing = [m for m in whatsapp_tool(container).sent if m.category == "marketing"]
        assert {m.to for m in marketing} == {CUSTOMER, "+919900445566"}
        assert services.cost_guard.spend_this_month() == pytest.approx(2 * 0.86)

    def test_budget_blocks_whole_batch_and_leaves_draft_retryable(
        self, container, session, pilot_context
    ):
        services = services_for(container, session)
        self.seed_audience(services, pilot_context, services.engagement_agent)
        draft = services.engagement_agent.draft_weekly_broadcast(pilot_context)
        approved, approval_log_id = services.state_machine.approve(draft.id, actor="owner")
        broke_guard = CostGuard(CostLedgerRepository(session, "pilot-1"), monthly_budget_inr=0.0)
        sends_before = len(whatsapp_tool(container).sent)
        with pytest.raises(BudgetExceededError):
            publish_draft(
                draft_id=approved.id,
                approval_log_id=approval_log_id,
                queue=services.queue,
                publish_log=services.publish_log,
                state_machine=services.state_machine,
                registry=container.registry,
                cost_guard=broke_guard,
            )
        # all-or-nothing: nothing sent, nothing published, draft safe to retry
        assert len(whatsapp_tool(container).sent) == sends_before
        assert services.publish_log.for_draft(draft.id) is None
        assert services.queue.get(draft.id).state == ApprovalState.APPROVED

    def test_republish_is_idempotent(self, container, session, pilot_context):
        services = services_for(container, session)
        self.seed_audience(services, pilot_context, services.engagement_agent)
        draft = services.engagement_agent.draft_weekly_broadcast(pilot_context)
        approved, approval_log_id = services.state_machine.approve(draft.id, actor="owner")
        kwargs = dict(
            draft_id=approved.id,
            approval_log_id=approval_log_id,
            queue=services.queue,
            publish_log=services.publish_log,
            state_machine=services.state_machine,
            registry=container.registry,
            cost_guard=services.cost_guard,
        )
        first = publish_draft(**kwargs)
        sends_after_first = len(whatsapp_tool(container).sent)
        second = publish_draft(**kwargs)
        assert second.external_ref == first.external_ref
        assert len(whatsapp_tool(container).sent) == sends_after_first


class TestMonthlyReportEnquiries:
    def test_report_counts_enquiries_handled(self, container, session, pilot_context):
        services = services_for(container, session)
        agent = services.engagement_agent
        agent.handle_inbound(pilot_context, CUSTOMER, "what are your hours?")
        agent.handle_inbound(pilot_context, CUSTOMER, "do you stock gluten-free flour?")
        now = datetime.now(UTC)
        report = services.insights_agent.monthly_report(pilot_context, now.year, now.month)
        assert "2 customer enquirie(s) came in on WhatsApp" in report
        assert "1 answered instantly, 1 passed to you" in report


class TestBspAdapter:
    def test_mock_stays_the_default_transport(self, container, pilot_context):
        assert isinstance(whatsapp_tool(container), MockWhatsAppTool)

    def test_credentials_switch_to_cloud_api_adapter(self):
        settings = make_test_settings()
        settings.whatsapp_bsp_api_key = "test-key"
        settings.whatsapp_phone_number_id = "1234567890"
        container = Container(settings)
        with container.session() as session:
            container.onboarding_agent(session).run("pilot-1", "bakery", PILOT_ANSWERS)
            services = container.services(session, "pilot-1")
            tool = container.registry.get("pilot-1", "whatsapp")
        assert isinstance(tool, CloudApiWhatsAppTool)
        assert services is not None

    def test_cloud_api_send_posts_expected_payload(self, monkeypatch):
        captured = {}

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"messages": [{"id": "wamid.TEST123"}]}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured.update(url=url, headers=headers, json=json)
            return FakeResponse()

        monkeypatch.setattr("localpulse.tools.whatsapp.httpx.post", fake_post)
        tool = CloudApiWhatsAppTool(client_id="pilot-1", api_key="k", phone_number_id="555")
        ref = tool.send(to=CUSTOMER, body="hello", category="service_reply")
        assert ref == "wamid.TEST123"
        assert captured["url"].endswith("/555/messages")
        assert captured["headers"]["Authorization"] == "Bearer k"
        assert captured["json"]["to"] == CUSTOMER
        assert captured["json"]["text"]["body"] == "hello"


class TestEngagementApi:
    @pytest.fixture
    def client(self):
        app = create_app(make_test_settings())
        client = TestClient(app)
        client.post(
            "/clients/pilot-1/onboard", json={"pack_ref": "bakery", "answers": PILOT_ANSWERS}
        )
        return client

    def test_inbound_endpoint_auto_answers(self, client):
        response = client.post(
            "/clients/pilot-1/engagement/inbound",
            json={"customer_number": CUSTOMER, "text": "what are your hours?"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["action"] == "faq"
        assert "8am-9pm" in payload["reply"]

    def test_broadcast_endpoint_422_without_audience(self, client):
        response = client.post("/clients/pilot-1/engagement/broadcast", json={})
        assert response.status_code == 422

    def test_broadcast_endpoint_returns_pending_draft(self, client):
        client.post(
            "/clients/pilot-1/engagement/inbound",
            json={"customer_number": CUSTOMER, "text": "menu please"},
        )
        response = client.post("/clients/pilot-1/engagement/broadcast", json={})
        assert response.status_code == 200
        draft = response.json()
        assert draft["kind"] == "whatsapp_broadcast"
        assert draft["state"] == "pending_approval"
        assert draft["meta"]["recipients"] == [CUSTOMER]
