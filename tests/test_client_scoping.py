"""Multi-tenant isolation: operations scoped to client A must never touch client B.
Cross-tenant leaks are the highest-severity failure (spec §12.1) — non-negotiable."""

import pytest

from localpulse.container import Container
from localpulse.context.models import ApprovalState, DraftItem, DraftKind
from localpulse.context.repositories import (
    ContentQueueRepository,
    MetricsRepository,
    NotFoundError,
)
from tests.conftest import PILOT_ANSWERS


@pytest.fixture
def two_clients(container: Container, session):
    agent = container.onboarding_agent(session)
    answers_b = {**PILOT_ANSWERS, "shop_name": "Sunrise Bakers", "owner_whatsapp": "+919899999999"}
    ctx_a = agent.run("client-a", "bakery", PILOT_ANSWERS)
    ctx_b = agent.run("client-b", "bakery", answers_b)
    return ctx_a, ctx_b


def _draft_for(client_id: str) -> DraftItem:
    return DraftItem(client_id=client_id, kind=DraftKind.GBP_POST, caption="hello")


def test_queue_reads_are_isolated(container, session, two_clients):
    queue_a = ContentQueueRepository(session, "client-a")
    queue_b = ContentQueueRepository(session, "client-b")
    draft = _draft_for("client-a")
    queue_a.add(draft)

    assert queue_b.list() == []
    with pytest.raises(NotFoundError):
        queue_b.get(draft.id)  # existence must not even be revealed
    assert queue_b.find_by_prefix(draft.id[:8]) is None
    assert queue_a.get(draft.id).id == draft.id


def test_repo_refuses_foreign_drafts_on_write(container, session, two_clients):
    queue_b = ContentQueueRepository(session, "client-b")
    with pytest.raises(ValueError):
        queue_b.add(_draft_for("client-a"))


def test_state_filtered_lists_are_isolated(container, session, two_clients):
    queue_a = ContentQueueRepository(session, "client-a")
    queue_b = ContentQueueRepository(session, "client-b")
    services_a = container.services(session, "client-a")
    submitted = services_a.state_machine.submit(_draft_for("client-a"))
    assert submitted.state == ApprovalState.PENDING_APPROVAL
    assert queue_b.list(state=ApprovalState.PENDING_APPROVAL) == []
    assert len(queue_a.list(state=ApprovalState.PENDING_APPROVAL)) == 1


def test_metrics_are_isolated(container, session, two_clients):
    metrics_a = MetricsRepository(session, "client-a")
    metrics_b = MetricsRepository(session, "client-b")
    metrics_a.record("profile_views", 42.0)
    assert metrics_b.latest("profile_views") is None
    assert metrics_a.latest("profile_views") == 42.0


def test_whatsapp_lookup_maps_to_correct_tenant(container, session, two_clients):
    from localpulse.context.repositories import ClientRepository

    clients = ClientRepository(session)
    found = clients.find_by_whatsapp("+919899999999")
    assert found is not None
    assert found.client_id == "client-b"
