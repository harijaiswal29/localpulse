"""Cost Guard — all outbound messaging routes through here (golden rule #4).

Picks the cheapest valid WhatsApp message category (mis-categorising a service
reply as marketing is the classic overspend) and enforces per-client budgets.
"""

from __future__ import annotations

from enum import StrEnum

from localpulse.context.repositories import CostLedgerRepository


class MessageCategory(StrEnum):
    SERVICE_REPLY = "service_reply"  # free within the 24h service window
    UTILITY = "utility"
    MARKETING = "marketing"


class MessagePurpose(StrEnum):
    REPLY = "reply"  # answering an inbound message
    APPROVAL_REQUEST = "approval_request"  # draft preview to the owner
    NOTIFICATION = "notification"  # reports, alerts
    MARKETING_BROADCAST = "marketing_broadcast"


# Indicative India pricing (INR per message); real rates come from the BSP.
CATEGORY_COST_INR: dict[MessageCategory, float] = {
    MessageCategory.SERVICE_REPLY: 0.0,
    MessageCategory.UTILITY: 0.32,
    MessageCategory.MARKETING: 0.86,
}


class BudgetExceededError(Exception):
    def __init__(self, client_id: str, budget_inr: float):
        super().__init__(f"client {client_id} exceeded monthly budget of ₹{budget_inr:.2f}")


def cheapest_valid_category(
    purpose: MessagePurpose, within_service_window: bool
) -> MessageCategory:
    """Prefer free service-window replies; never send a marketing template where a
    service reply works (golden rule #4)."""
    if purpose == MessagePurpose.MARKETING_BROADCAST:
        return MessageCategory.MARKETING
    if within_service_window:
        return MessageCategory.SERVICE_REPLY
    return MessageCategory.UTILITY


class CostGuard:
    def __init__(self, ledger: CostLedgerRepository, monthly_budget_inr: float):
        self._ledger = ledger
        self._budget = monthly_budget_inr

    def charge(self, category: MessageCategory, note: str = "") -> float:
        """Authorise + record one outbound message. Raises before sending if the
        client's monthly budget would be exceeded (circuit breaker, spec §12.1)."""
        cost = CATEGORY_COST_INR[category]
        if cost > 0 and self._ledger.spend_this_month() + cost > self._budget:
            raise BudgetExceededError(self._ledger.client_id, self._budget)
        self._ledger.record(category.value, cost, note)
        return cost

    def spend_this_month(self) -> float:
        return self._ledger.spend_this_month()
