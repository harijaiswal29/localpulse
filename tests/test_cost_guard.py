"""Golden rule #4: cheapest valid category, budgets enforced, marketing never
substituted for a service reply."""

import pytest

from localpulse.container import Container
from localpulse.context.repositories import CostLedgerRepository
from localpulse.orchestrator.cost_guard import (
    BudgetExceededError,
    CostGuard,
    MessageCategory,
    MessagePurpose,
    cheapest_valid_category,
)


def test_reply_in_window_is_free_service_reply():
    assert (
        cheapest_valid_category(MessagePurpose.REPLY, within_service_window=True)
        == MessageCategory.SERVICE_REPLY
    )


def test_approval_request_in_window_is_free():
    assert (
        cheapest_valid_category(MessagePurpose.APPROVAL_REQUEST, within_service_window=True)
        == MessageCategory.SERVICE_REPLY
    )


def test_never_marketing_when_service_reply_works():
    purposes = [
        MessagePurpose.REPLY,
        MessagePurpose.APPROVAL_REQUEST,
        MessagePurpose.NOTIFICATION,
    ]
    for purpose in purposes:
        assert (
            cheapest_valid_category(purpose, within_service_window=True)
            != MessageCategory.MARKETING
        )


def test_outside_window_uses_utility_not_marketing():
    assert (
        cheapest_valid_category(MessagePurpose.NOTIFICATION, within_service_window=False)
        == MessageCategory.UTILITY
    )


def test_broadcast_is_marketing():
    assert (
        cheapest_valid_category(MessagePurpose.MARKETING_BROADCAST, within_service_window=True)
        == MessageCategory.MARKETING
    )


def test_budget_blocks_paid_sends(container: Container, session):
    ledger = CostLedgerRepository(session, "pilot-1")
    guard = CostGuard(ledger, monthly_budget_inr=1.0)
    guard.charge(MessageCategory.MARKETING)  # ₹0.86 — fits
    with pytest.raises(BudgetExceededError):
        guard.charge(MessageCategory.MARKETING)  # would exceed ₹1.00


def test_free_messages_never_blocked_by_budget(container: Container, session):
    ledger = CostLedgerRepository(session, "pilot-1")
    guard = CostGuard(ledger, monthly_budget_inr=0.0)
    for _ in range(5):
        assert guard.charge(MessageCategory.SERVICE_REPLY) == 0.0


def test_spend_is_recorded(container: Container, session):
    ledger = CostLedgerRepository(session, "pilot-1")
    guard = CostGuard(ledger, monthly_budget_inr=100.0)
    guard.charge(MessageCategory.MARKETING)
    guard.charge(MessageCategory.UTILITY)
    assert guard.spend_this_month() == pytest.approx(0.86 + 0.32)
