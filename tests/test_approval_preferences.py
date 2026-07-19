"""P3 approval preferences tests — owner-configurable A1 → A0 promotions.

The owner can promote trusted draft kinds to publish without a per-item tap.
Every promotion still runs through the Approval State Machine and is logged
(actor "owner_preference"), and A2-escalated drafts are never auto-approved,
whatever the preference says.
"""

from datetime import date

from fastapi.testclient import TestClient

from localpulse.agents.content import ContentTrigger
from localpulse.api import main as api_main
from localpulse.api.main import create_app
from localpulse.context.models import ApprovalState, DraftKind
from localpulse.context.repositories import ClientRepository, CostLedgerRepository
from localpulse.orchestrator.approval import PREFERENCE_ACTOR
from localpulse.orchestrator.cost_guard import CostGuard
from localpulse.orchestrator.publisher import publish_ready
from localpulse.orchestrator.router import TaskRouter
from localpulse.tools.gbp import Review
from tests.conftest import PILOT_ANSWERS, make_test_settings

WEEK = date(2026, 7, 27)
CUSTOMER = "+919900112233"


def enable_auto(session, kinds: list[DraftKind]):
    clients = ClientRepository(session)
    ctx = clients.get("pilot-1")
    ctx.approval_prefs.auto_publish_kinds = kinds
    clients.save(ctx)
    return ctx


class TestAutoApproval:
    def test_default_everything_waits_for_the_owner(self, container, session, pilot_context):
        services = container.services(session, "pilot-1")
        drafts = services.content_agent.run(pilot_context, ContentTrigger(week_start=WEEK))
        assert drafts
        assert all(d.state == ApprovalState.PENDING_APPROVAL for d in drafts)

    def test_trusted_kind_is_auto_approved_with_full_audit_trail(
        self, container, session, pilot_context
    ):
        ctx = enable_auto(session, [DraftKind.GBP_POST])
        services = container.services(session, "pilot-1")
        drafts = services.content_agent.run(ctx, ContentTrigger(week_start=WEEK))
        assert all(d.state == ApprovalState.APPROVED for d in drafts)
        # the standing preference is a logged decision, not a bypass
        entries = services.approval_log.for_draft(drafts[0].id)
        transitions = [(e.from_state, e.to_state, e.actor) for e in entries]
        assert ("drafted", "pending_approval", "content_agent") in transitions
        assert ("pending_approval", "approved", PREFERENCE_ACTOR) in transitions

    def test_publish_ready_delivers_and_is_idempotent(self, container, session, pilot_context):
        ctx = enable_auto(session, [DraftKind.GBP_POST])
        services = container.services(session, "pilot-1")
        drafts = services.content_agent.run(ctx, ContentTrigger(week_start=WEEK))
        actions = publish_ready(services, container.registry)
        assert len(actions) == len(drafts)
        gbp = container.registry.get("pilot-1", "gbp")
        assert len(gbp.queue) == len(drafts)
        assert publish_ready(services, container.registry) == []  # nothing left over
        assert all(services.queue.get(d.id).state == ApprovalState.PUBLISHED for d in drafts)

    def test_owner_notification_separates_auto_from_pending(
        self, container, session, pilot_context
    ):
        ctx = enable_auto(session, [DraftKind.GBP_POST])
        services = container.services(session, "pilot-1")
        services.content_agent.run(ctx, ContentTrigger(week_start=WEEK))
        body = container.registry.get("pilot-1", "whatsapp").sent[-1].body
        assert "publishing automatically" in body
        assert "Reply APPROVE" not in body  # nothing is waiting on the owner


class TestEscalationsNeverAutoPublish:
    def seed_review(self, container, rating: int, text: str):
        gbp = container.registry.get("pilot-1", "gbp")
        gbp.reviews.append(
            Review(review_id=f"rev-{rating}", rating=rating, text=text, language="en", author="X")
        )

    def test_negative_review_reply_stays_pending_even_when_kind_is_trusted(
        self, container, session, pilot_context
    ):
        ctx = enable_auto(session, [DraftKind.REVIEW_REPLY])
        services = container.services(session, "pilot-1")
        self.seed_review(container, 2, "Stale cake, waited forever. Disappointed.")
        (draft,) = services.reputation_agent.check_reviews(ctx)
        assert draft.meta["escalated"] is True
        assert draft.state == ApprovalState.PENDING_APPROVAL  # A2: human decision required
        assert publish_ready(services, container.registry) == []

    def test_positive_review_reply_publishes_on_the_standing_preference(
        self, container, session, pilot_context
    ):
        ctx = enable_auto(session, [DraftKind.REVIEW_REPLY])
        services = container.services(session, "pilot-1")
        self.seed_review(container, 5, "The modak box was perfect!")
        (draft,) = services.reputation_agent.check_reviews(ctx)
        assert draft.state == ApprovalState.APPROVED
        actions = publish_ready(services, container.registry)
        assert len(actions) == 1
        assert container.registry.get("pilot-1", "gbp").reply_queue[0].reply == draft.caption


class TestBudgetSafety:
    def test_blocked_broadcast_stays_approved_and_retries(self, container, session, pilot_context):
        ctx = enable_auto(session, [DraftKind.WHATSAPP_BROADCAST])
        services = container.services(session, "pilot-1")
        services.engagement_agent.handle_inbound(ctx, CUSTOMER, "what are your hours?")
        draft = services.engagement_agent.draft_weekly_broadcast(ctx)
        assert draft.state == ApprovalState.APPROVED  # standing preference applied

        services.cost_guard = CostGuard(CostLedgerRepository(session, "pilot-1"), 0.0)
        assert publish_ready(services, container.registry) == []  # blocked, not lost
        assert services.queue.get(draft.id).state == ApprovalState.APPROVED

        services.cost_guard = CostGuard(CostLedgerRepository(session, "pilot-1"), 500.0)
        (action,) = publish_ready(services, container.registry)
        assert action.external_ref == "wa-broadcast:1"


class TestWorkerDelivery:
    def test_dispatch_publishes_what_the_preference_approved(
        self, container, session, pilot_context
    ):
        enable_auto(session, [DraftKind.GBP_POST])
        router = TaskRouter(container)
        assert router.dispatch("pilot-1", "content.generate_week") is True
        gbp = container.registry.get("pilot-1", "gbp")
        assert len(gbp.queue) == 3  # the whole week went out on autopilot


class TestOwnerConfiguration:
    def test_whatsapp_auto_command_round_trip(self, container, session, pilot_context):
        services = container.services(session, "pilot-1")
        clients = ClientRepository(session)

        reply = api_main._handle_owner_command(services, container, clients, "AUTO")
        assert "nothing" in reply

        reply = api_main._handle_owner_command(services, container, clients, "AUTO ON gbp_post")
        assert "automatically" in reply
        assert clients.get("pilot-1").approval_prefs.auto_publish_kinds == [DraftKind.GBP_POST]

        reply = api_main._handle_owner_command(services, container, clients, "AUTO ON nonsense")
        assert "isn't a draft kind" in reply

        reply = api_main._handle_owner_command(services, container, clients, "AUTO OFF gbp_post")
        assert "wait for your approval" in reply
        assert clients.get("pilot-1").approval_prefs.auto_publish_kinds == []

    def test_api_endpoint_sets_preferences_and_validates_kinds(self):
        client = TestClient(create_app(make_test_settings()))
        client.post(
            "/clients/pilot-1/onboard",
            json={"pack_ref": "bakery", "answers": PILOT_ANSWERS},
        )
        response = client.put(
            "/clients/pilot-1/approval-preferences",
            json={"auto_publish_kinds": ["gbp_post", "review_reply", "gbp_post"]},
        )
        assert response.status_code == 200
        assert response.json()["approval_prefs"]["auto_publish_kinds"] == [
            "gbp_post",
            "review_reply",
        ]
        assert (
            client.put(
                "/clients/pilot-1/approval-preferences",
                json={"auto_publish_kinds": ["telegram_post"]},
            ).status_code
            == 422
        )

    def test_api_content_run_reports_auto_published_drafts(self):
        client = TestClient(create_app(make_test_settings()))
        client.post(
            "/clients/pilot-1/onboard",
            json={"pack_ref": "bakery", "answers": PILOT_ANSWERS},
        )
        client.put(
            "/clients/pilot-1/approval-preferences",
            json={"auto_publish_kinds": ["gbp_post"]},
        )
        response = client.post(
            "/clients/pilot-1/content/run", json={"week_start": WEEK.isoformat()}
        )
        payload = response.json()
        assert payload["auto_published"] == len(payload["drafts"]) > 0
        assert all(d["state"] == "published" for d in payload["drafts"])
