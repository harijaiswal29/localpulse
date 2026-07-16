"""WhatsApp tool (via BSP). The mock is the default offline transport; the Cloud
API adapter below is the real BSP client, picked by the container only when
credentials are configured. All sends are cost-guarded upstream — this layer
never decides message category itself."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)


@dataclass
class OutboundMessage:
    to: str
    body: str
    category: str


class WhatsAppTool(Protocol):
    def send(self, to: str, body: str, category: str) -> str: ...


@dataclass
class MockWhatsAppTool:
    """Logs and records messages instead of sending — pilot / test transport."""

    client_id: str
    sent: list[OutboundMessage] = field(default_factory=list)

    def send(self, to: str, body: str, category: str) -> str:
        message = OutboundMessage(to=to, body=body, category=category)
        self.sent.append(message)
        logger.info("[whatsapp:%s] -> %s (%s): %s", self.client_id, to, category, body)
        return f"wa-mock:{len(self.sent)}"


@dataclass
class CloudApiWhatsAppTool:
    """WhatsApp Business Cloud API adapter (Meta's first-party BSP).

    Pilot-thin: sends free-form text, which the platform accepts inside the 24h
    service window — exactly where the Engagement Agent operates. Utility and
    marketing sends outside the window need pre-approved templates; template
    management lands when a pilot outgrows the service window (spec §7).
    """

    client_id: str
    api_key: str
    phone_number_id: str
    base_url: str = "https://graph.facebook.com/v20.0"

    def send(self, to: str, body: str, category: str) -> str:
        response = httpx.post(
            f"{self.base_url}/{self.phone_number_id}/messages",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"body": body},
            },
            timeout=30,
        )
        response.raise_for_status()
        message_id = response.json()["messages"][0]["id"]
        logger.info(
            "[whatsapp:%s] -> %s (%s) via cloud api: %s", self.client_id, to, category, message_id
        )
        return message_id
