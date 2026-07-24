"""Single choke point for outbound WhatsApp: category selection, the template rule
and the budget check all happen here, so no agent can bypass the Cost Guard
(golden rule #4) or hand WhatsApp something it will refuse to deliver."""

from __future__ import annotations

import logging

from localpulse.context.models import ClientContext, TemplateSlot
from localpulse.orchestrator.cost_guard import (
    CostGuard,
    MessageCategory,
    MessagePurpose,
    cheapest_valid_category,
)
from localpulse.orchestrator.templates import TemplateRequiredError, render
from localpulse.packs.base import VerticalPack
from localpulse.tools.whatsapp import OutboundTemplate, WhatsAppTool

logger = logging.getLogger(__name__)


def send_whatsapp(
    guard: CostGuard,
    tool: WhatsAppTool,
    to: str,
    purpose: MessagePurpose,
    within_service_window: bool,
    body: str = "",
    template: OutboundTemplate | None = None,
) -> str:
    """Send one WhatsApp message, the cheapest legal way.

    Inside the 24h service window free-form text is delivered and costs nothing, so
    that is what goes out — even when a template was rendered for it. Outside the
    window WhatsApp delivers approved templates only, so a paid send without one is
    refused here rather than failing at the BSP.

    The budget is authorised before the send and charged after it, so the ledger
    counts messages that actually left — a send the transport rejects costs the
    shop nothing.
    """
    category = cheapest_valid_category(purpose, within_service_window)
    text = template.body if template is not None else body
    if not text.strip():
        raise ValueError("refusing to send an empty WhatsApp message")
    if category is MessageCategory.SERVICE_REPLY:
        template = None  # same words, no template fee
    elif template is None:
        raise TemplateRequiredError(category, purpose.value)
    guard.ensure_affordable(category)  # raises BudgetExceededError before sending
    external_ref = tool.send(to=to, body=text, category=category.value, template=template)
    guard.charge(category, note=purpose.value)
    return external_ref


def notify_owner(
    guard: CostGuard,
    tool: WhatsAppTool,
    ctx: ClientContext,
    pack: VerticalPack,
    body: str,
    purpose: MessagePurpose,
    window_open: bool,
    summary: str = "",
) -> str | None:
    """Tell the owner something — a draft digest, an escalation, the monthly report.

    Inside the owner's window the full text goes out free-form, as before. Outside
    it, only a template is deliverable and a template parameter cannot carry
    newlines, so a multi-line digest physically cannot be sent as-is: we send the
    pack's short owner-alert instead, which re-opens the window so the detail is one
    reply away. The queue is the source of truth for the detail either way.
    """
    if window_open:
        return send_whatsapp(
            guard=guard,
            tool=tool,
            to=ctx.business.owner_whatsapp,
            body=body,
            purpose=purpose,
            within_service_window=True,
        )
    template = pack.message_template(TemplateSlot.OWNER_ALERT)
    if template is None:
        logger.warning(
            "[messaging:%s] pack %r has no owner-alert template — the owner cannot be "
            "reached outside the service window",
            ctx.client_id,
            pack.ref,
        )
        return None
    rendered = render(
        template,
        {"business_name": ctx.business.name, "summary": summary or _headline(body)},
    )
    return send_whatsapp(
        guard=guard,
        tool=tool,
        to=ctx.business.owner_whatsapp,
        purpose=purpose,
        within_service_window=False,
        template=rendered,
    )


def _headline(body: str) -> str:
    """First line of a notification, as a one-line template parameter."""
    lines = [line for line in body.strip().splitlines() if line.strip()]
    return lines[0][:180] if lines else "you have an update waiting"
