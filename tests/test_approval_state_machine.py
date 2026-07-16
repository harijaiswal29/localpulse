"""Golden rule #1: nothing publishes without passing the Approval State Machine.
Every transition tested; illegal transitions rejected."""

from datetime import UTC, datetime, timedelta

import pytest

from localpulse.container import Container
from localpulse.context.models import ApprovalState, DraftItem, DraftKind
from localpulse.orchestrator.approval import (
    LEGAL_TRANSITIONS,
    IllegalTransitionError,
    validate_transition,
)
from localpulse.orchestrator.publisher import NotApprovedError, publish_draft

ALL_STATES = list(ApprovalState)


def make_draft(client_id: str = "pilot-1", **kwargs) -> DraftItem:
    defaults = dict(client_id=client_id, kind=DraftKind.GBP_POST, caption="Fresh bread today!")
    defaults.update(kwargs)
    return DraftItem(**defaults)


def test_every_legal_transition_is_accepted():
    for current, targets in LEGAL_TRANSITIONS.items():
        for target in targets:
            validate_transition(current, target)  # must not raise


def test_every_illegal_transition_is_rejected():
    for current in ALL_STATES:
        for target in ALL_STATES:
            if target in LEGAL_TRANSITIONS[current]:
                continue
            with pytest.raises(IllegalTransitionError):
                validate_transition(current, target)


def test_drafted_can_never_jump_straight_to_published():
    with pytest.raises(IllegalTransitionError):
        validate_transition(ApprovalState.DRAFTED, ApprovalState.PUBLISHED)


def test_full_happy_path(container: Container, session, pilot_context):
    services = container.services(session, "pilot-1")
    draft = services.state_machine.submit(make_draft())
    assert draft.state == ApprovalState.PENDING_APPROVAL

    draft, approval_log_id = services.state_machine.approve(draft.id, actor="owner")
    assert draft.state == ApprovalState.APPROVED

    draft = services.state_machine.mark_published(draft.id)
    assert draft.state == ApprovalState.PUBLISHED

    log = services.approval_log.for_draft(draft.id)
    states = [(entry.from_state, entry.to_state) for entry in log]
    assert ("pending_approval", "approved") in states
    assert ("approved", "published") in states


def test_reject_discards(container: Container, session, pilot_context):
    services = container.services(session, "pilot-1")
    draft = services.state_machine.submit(make_draft())
    draft = services.state_machine.reject(draft.id, actor="owner")
    assert draft.state == ApprovalState.DISCARDED


def test_edit_keeps_item_pending_and_logs(container: Container, session, pilot_context):
    services = container.services(session, "pilot-1")
    draft = services.state_machine.submit(make_draft(caption="Original"))
    edited = services.state_machine.edit(draft.id, "Better caption", actor="owner")
    assert edited.state == ApprovalState.PENDING_APPROVAL
    assert edited.caption == "Better caption"
    assert edited.meta["original_caption"] == "Original"


def test_timeout_never_auto_publishes(container: Container, session, pilot_context):
    services = container.services(session, "pilot-1")
    past = datetime.now(UTC) - timedelta(hours=1)
    draft = services.state_machine.submit(make_draft(expires_at=past, time_sensitive=True))
    expired = services.state_machine.sweep_expired()
    assert len(expired) == 1
    assert expired[0].state == ApprovalState.DISCARDED  # time-sensitive: expire + discard
    assert services.queue.get(draft.id).state != ApprovalState.PUBLISHED


def test_evergreen_expiry_is_not_discarded(container: Container, session, pilot_context):
    services = container.services(session, "pilot-1")
    past = datetime.now(UTC) - timedelta(hours=1)
    services.state_machine.submit(make_draft(expires_at=past, time_sensitive=False))
    expired = services.state_machine.sweep_expired()
    assert expired[0].state == ApprovalState.EXPIRED  # eligible for re-notify, not lost


def test_publish_refuses_unapproved_draft(container: Container, session, pilot_context):
    """Red-team: adversarial attempt to publish without approval must fail closed."""
    services = container.services(session, "pilot-1")
    draft = services.state_machine.submit(make_draft())
    with pytest.raises(NotApprovedError):
        publish_draft(
            draft_id=draft.id,
            approval_log_id=1,
            queue=services.queue,
            publish_log=services.publish_log,
            state_machine=services.state_machine,
            registry=container.registry,
        )
    assert services.queue.get(draft.id).state == ApprovalState.PENDING_APPROVAL


def test_publish_is_idempotent(container: Container, session, pilot_context):
    services = container.services(session, "pilot-1")
    draft = services.state_machine.submit(make_draft())
    _, approval_log_id = services.state_machine.approve(draft.id, actor="owner")
    kwargs = dict(
        draft_id=draft.id,
        approval_log_id=approval_log_id,
        queue=services.queue,
        publish_log=services.publish_log,
        state_machine=services.state_machine,
        registry=container.registry,
    )
    first = publish_draft(**kwargs)
    second = publish_draft(**kwargs)  # retry after "publish failed after approval"
    assert first.external_ref == second.external_ref
    gbp = container.registry.get("pilot-1", "gbp")
    assert len(gbp.queue) == 1
