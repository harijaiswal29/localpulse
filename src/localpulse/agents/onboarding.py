"""Onboarding Agent (A1) — turns a messy real-world shop into a structured
Client Context. The question set comes from the Vertical Pack; the mapping of
answers to context fields is generic engine code."""

from __future__ import annotations

import re

from localpulse.context.models import (
    BrandVoice,
    BusinessProfile,
    Channel,
    ChannelStatus,
    ClientContext,
    Offering,
)
from localpulse.context.regional_calendar import regional_calendar
from localpulse.context.repositories import ClientRepository
from localpulse.packs.base import load_pack

_PRICE_PATTERN = re.compile(r"[₹Rr][sS]?\.?\s*(\d+(?:\.\d+)?)")


class OnboardingIncompleteError(Exception):
    def __init__(self, missing: list[str]):
        super().__init__(f"missing required onboarding answers: {', '.join(missing)}")
        self.missing = missing


def _split_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _parse_offerings(value: str) -> list[Offering]:
    offerings = []
    for item in _split_list(value):
        match = _PRICE_PATTERN.search(item)
        price = float(match.group(1)) if match else None
        name = _PRICE_PATTERN.sub("", item).strip(" -–@")
        if name:
            offerings.append(Offering(name=name, price_inr=price))
    return offerings


class OnboardingAgent:
    task_profile = "router"

    def __init__(self, clients: ClientRepository):
        self._clients = clients

    def run(self, client_id: str, pack_ref: str, answers: dict[str, str]) -> ClientContext:
        pack = load_pack(pack_ref)
        missing = [
            q.id
            for q in pack.onboarding_questions
            if q.required and not answers.get(q.id, "").strip()
        ]
        if missing:
            raise OnboardingIncompleteError(missing)

        by_field = {
            q.field: answers[q.id].strip()
            for q in pack.onboarding_questions
            if answers.get(q.id, "").strip()
        }

        business = BusinessProfile(
            name=by_field.get("business.name", ""),
            category=pack.display_name,
            address=by_field.get("business.address", ""),
            city=by_field.get("business.city", ""),
            hours={"daily": by_field["business.hours"]} if "business.hours" in by_field else {},
            phone=by_field.get("business.phone", ""),
            owner_whatsapp=by_field.get("business.owner_whatsapp", ""),
        )
        brand_voice = BrandVoice(
            tone=_split_list(by_field.get("brand_voice.tone", "")),
            languages=_split_list(by_field.get("brand_voice.languages", "")) or ["en"],
            example_posts=(
                [by_field["brand_voice.example_posts"]]
                if "brand_voice.example_posts" in by_field
                else []
            ),
        )
        offerings = _parse_offerings(by_field.get("offerings.products", ""))
        notes = {
            field.removeprefix("notes."): value
            for field, value in by_field.items()
            if field.startswith("notes.")
        }

        context = ClientContext(
            client_id=client_id,
            vertical_pack_ref=pack_ref,
            business=business,
            brand_voice=brand_voice,
            offerings=offerings,
            calendar=regional_calendar(),
            channels=[
                ChannelStatus(channel=Channel.WHATSAPP, connected=True),
                # GBP marked connected in semi-manual mode; real OAuth comes with API access
                ChannelStatus(channel=Channel.GBP, connected=True),
            ],
            notes=notes,
        )
        self._clients.save(context)
        return context
