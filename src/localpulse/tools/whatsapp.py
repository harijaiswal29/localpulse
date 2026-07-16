"""WhatsApp tool (via BSP). P0 runs a console/mock transport; the typed interface
matches what a real BSP client needs. All sends are cost-guarded upstream — this
layer never decides message category itself."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

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
