"""Model gateway — agents call this, never a vendor SDK (golden rule #7).

Each agent declares a task profile ("content", "router", "insights"); config maps
profiles -> models; the gateway resolves the call. Any model may run an agent,
provided it passes that agent's eval bar (spec §13.1).
"""

from __future__ import annotations

import re
from typing import Protocol

import httpx

from localpulse.config import Settings


class ModelProvider(Protocol):
    def complete(self, prompt: str, system: str, max_tokens: int) -> str: ...


class MockProvider:
    """Deterministic offline provider for dev/tests. Reads `key: value` lines from
    the prompt and templates a grounded caption — no network, no key, no cost."""

    def complete(self, prompt: str, system: str, max_tokens: int) -> str:
        facts = dict(re.findall(r"^(\w+):\s*(.+)$", prompt, flags=re.MULTILINE))
        business = facts.get("business", "our shop")
        if "reviewer" in facts:
            return self._review_reply(facts, business)[: max_tokens * 4]
        if "customer" in facts:
            return (
                f"Hi {facts['customer']}! Thank you for choosing {business}. "
                f"If you enjoyed your order, a quick Google review would mean a lot to us — "
                f"just search for {business} on Google Maps. 🙏"
            )[: max_tokens * 4]
        if "offer" in facts:
            return (
                f"This week at {business}: {facts['offer']}! Reply right here to "
                f"grab yours before the weekend rush. ✨"
            )[: max_tokens * 4]
        offering = facts.get("offering", "")
        occasion = facts.get("occasion", "")
        hook = facts.get("hook", "something special")
        parts: list[str] = []
        if occasion:
            parts.append(f"Happy {occasion}!")
        if offering:
            parts.append(f"{offering} at {business} — {hook}.")
        else:
            parts.append(f"{business} — {hook}.")
        parts.append("Message us on WhatsApp to book or order.")
        return " ".join(parts)[: max_tokens * 4]

    @staticmethod
    def _review_reply(facts: dict[str, str], business: str) -> str:
        reviewer = facts["reviewer"]
        sentiment = facts.get("sentiment", "positive")
        if sentiment in {"negative", "ambiguous"}:
            return (
                f"We're really sorry, {reviewer} — this isn't the experience we want anyone "
                f"to have at {business}. Please message us on WhatsApp so we can make it right."
            )
        return (
            f"Thank you so much, {reviewer}! We're delighted you enjoyed it — "
            f"see you again soon at {business}. 🙏"
        )


class AnthropicProvider:
    """Thin HTTP adapter (no vendor SDK dependency in agent code)."""

    def __init__(self, api_key: str, model: str):
        self._api_key = api_key
        self._model = model

    def complete(self, prompt: str, system: str, max_tokens: int) -> str:
        response = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": self._model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        response.raise_for_status()
        return "".join(
            block["text"] for block in response.json()["content"] if block["type"] == "text"
        )


class ModelGateway:
    def __init__(self, model_map: dict[str, str], anthropic_api_key: str = ""):
        self._model_map = model_map
        self._anthropic_api_key = anthropic_api_key

    @classmethod
    def from_settings(cls, settings: Settings) -> ModelGateway:
        return cls(settings.model_map(), settings.anthropic_api_key)

    def model_for(self, task_profile: str) -> str:
        return self._model_map.get(task_profile, "mock")

    def complete(
        self, task_profile: str, prompt: str, system: str = "", max_tokens: int = 512
    ) -> str:
        return self._provider_for(self.model_for(task_profile)).complete(prompt, system, max_tokens)

    def _provider_for(self, model: str) -> ModelProvider:
        if model == "mock" or not model:
            return MockProvider()
        if model.startswith("claude") and self._anthropic_api_key:
            return AnthropicProvider(self._anthropic_api_key, model)
        # Unknown model with no configured provider: fail closed to the offline mock
        # rather than guessing a vendor. Add providers (OpenRouter/Ollama/...) here.
        return MockProvider()
