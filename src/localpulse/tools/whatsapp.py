"""WhatsApp tool (via BSP). The mock is the default offline transport; the Cloud
API adapter below is the real BSP client, picked by the container only when
credentials are configured. All sends are cost-guarded upstream — this layer
never decides message category itself."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from localpulse.tools.retry import (
    DEFAULT_POLICY,
    RetryPolicy,
    call_with_retry,
    raise_for_response,
    transient_from,
)

logger = logging.getLogger(__name__)


@dataclass
class OutboundTemplate:
    """A rendered message template, ready for the wire.

    `body` is the rendered text — what the owner approved and what the mock records;
    `params` is the same content in the positional form the Cloud API expects.
    """

    name: str
    language: str
    params: list[str]
    body: str


@dataclass
class OutboundMessage:
    to: str
    body: str
    category: str
    template: str = ""  # template name when sent as a template, "" for free-form text


class WhatsAppTool(Protocol):
    def send(
        self, to: str, body: str, category: str, template: OutboundTemplate | None = None
    ) -> str: ...


@dataclass
class MockWhatsAppTool:
    """Logs and records messages instead of sending — pilot / test transport."""

    client_id: str
    sent: list[OutboundMessage] = field(default_factory=list)

    def send(
        self, to: str, body: str, category: str, template: OutboundTemplate | None = None
    ) -> str:
        message = OutboundMessage(
            to=to, body=body, category=category, template=template.name if template else ""
        )
        self.sent.append(message)
        logger.info("[whatsapp:%s] -> %s (%s): %s", self.client_id, to, category, body)
        return f"wa-mock:{len(self.sent)}"


@dataclass
class CloudApiWhatsAppTool:
    """WhatsApp Business Cloud API adapter (Meta's first-party BSP).

    Free-form text inside the 24h service window, a pre-approved template outside
    it — the two things the platform actually accepts. Which one applies is decided
    upstream by the Cost Guard's category, never here.
    """

    client_id: str
    api_key: str
    phone_number_id: str
    base_url: str = "https://graph.facebook.com/v20.0"
    retry: RetryPolicy = DEFAULT_POLICY

    def send(
        self, to: str, body: str, category: str, template: OutboundTemplate | None = None
    ) -> str:
        """Deliver one message, retrying a rate-limited or failing API (spec §12.1).

        A retry here re-sends the same message, so it must only ever happen when
        the previous attempt did *not* deliver: `raise_for_response` retries the
        statuses Meta uses for "not accepted, try again" and treats everything
        else as permanent.
        """
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
        }
        payload.update(self._text(body) if template is None else self._template(template))
        return call_with_retry(
            f"whatsapp.send -> {to}",
            lambda: self._post(payload, to, category, template),
            policy=self.retry,
        )

    def _post(
        self, payload: dict, to: str, category: str, template: OutboundTemplate | None
    ) -> str:
        operation = "whatsapp.send"
        try:
            response = httpx.post(
                f"{self.base_url}/{self.phone_number_id}/messages",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=30,
            )
        except httpx.RequestError as exc:
            raise transient_from(exc, operation) from exc
        raise_for_response(response, operation)
        message_id = response.json()["messages"][0]["id"]
        logger.info(
            "[whatsapp:%s] -> %s (%s%s) via cloud api: %s",
            self.client_id,
            to,
            category,
            f", template {template.name}" if template else "",
            message_id,
        )
        return message_id

    @staticmethod
    def _text(body: str) -> dict:
        return {"type": "text", "text": {"body": body}}

    @staticmethod
    def _template(template: OutboundTemplate) -> dict:
        message: dict = {
            "type": "template",
            "template": {
                "name": template.name,
                "language": {"code": template.language},
            },
        }
        if template.params:
            message["template"]["components"] = [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": p} for p in template.params],
                }
            ]
        return message
