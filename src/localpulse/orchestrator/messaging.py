"""Single choke point for outbound WhatsApp: category selection + budget check
happen here, so no agent can bypass the Cost Guard (golden rule #4)."""

from __future__ import annotations

from localpulse.orchestrator.cost_guard import (
    CostGuard,
    MessagePurpose,
    cheapest_valid_category,
)
from localpulse.tools.whatsapp import WhatsAppTool


def send_whatsapp(
    guard: CostGuard,
    tool: WhatsAppTool,
    to: str,
    body: str,
    purpose: MessagePurpose,
    within_service_window: bool,
) -> str:
    category = cheapest_valid_category(purpose, within_service_window)
    guard.charge(category, note=purpose.value)  # raises BudgetExceededError before sending
    return tool.send(to=to, body=body, category=category.value)
