"""Vertical Pack contract. ALL vertical-specific logic lives in packs (golden rule #2).

A pack is a package under localpulse.packs.<ref> exporting PACK: VerticalPack with
templates, onboarding_questions, offering_schema, calendar_weights, playbook, guardrails.
The engine loads a pack by client_context.vertical_pack_ref.
"""

from __future__ import annotations

import importlib
import re

from pydantic import BaseModel

from localpulse.context.models import OfferingType


class ContentTemplate(BaseModel):
    id: str
    hook: str  # the angle, e.g. "daily special", "festival greeting"
    prompt: str  # instruction fragment handed to the model gateway
    occasion: str | None = None  # "festival" templates bind to a calendar event
    requires_offering: bool = False


class OnboardingQuestion(BaseModel):
    id: str
    question: str
    field: str  # Client Context field path this answer populates
    required: bool = True


class OfferingSchema(BaseModel):
    allowed_types: list[OfferingType]
    required_fields: list[str] = ["name"]
    # vertical judgement: offerings in this vertical are booked by appointment
    # (salon, clinic) rather than picked up or delivered (bakery, boutique)
    requires_appointment: bool = False


class CadenceRule(BaseModel):
    """General cadence entry (cron), not a hard-coded weekly rhythm — long nurture
    sequences must be able to slot in later (spec §2.2)."""

    task: str
    cron: str


class FaqEntry(BaseModel):
    """A deterministic FAQ the Engagement Agent may auto-answer (A0). The answer is
    a template filled from the Client Context — placeholders like {business_name},
    {hours}, {address}, {city}, {phone}, {menu} — so it can never hallucinate."""

    id: str
    patterns: list[str]  # lowercase substrings that trigger this FAQ
    answer: str


class EngagementPlaybook(BaseModel):
    """Pack-driven WhatsApp behaviour (spec §5.4). The matching machinery is engine
    code; every cue, template, and vertical judgement call lives here in the pack."""

    faqs: list[FaqEntry] = []
    preorder_cues: list[str] = []  # substrings that signal an order/booking attempt
    # words too generic to identify a single offering on their own (e.g. "cake") —
    # a pre-order mentioning only these escalates instead of guessing
    vague_terms: list[str] = []
    # template placeholders: {offering_name} {price} {customer_name} + context fields
    preorder_ack: str = ""
    escalation_ack: str = ""  # holding reply while the owner is looped in
    opt_out_ack: str = ""  # confirmation after STOP/unsubscribe
    broadcast_prompt: str = ""  # instruction for the weekly offer broadcast draft


class Playbook(BaseModel):
    posts_per_week: int = 3
    post_weekdays: list[int] = [1, 3, 5]  # ISO weekday (1=Mon)
    cadence: list[CadenceRule] = []
    review_reply_style: str = ""
    engagement: EngagementPlaybook = EngagementPlaybook()


class Guardrails(BaseModel):
    banned_terms: list[str] = []
    forbid_health_claims: bool = False
    max_caption_chars: int = 700
    require_offering_grounding: bool = True


class VerticalPack(BaseModel):
    ref: str
    display_name: str
    family: int  # rollout family per spec §2.1
    templates: list[ContentTemplate]
    onboarding_questions: list[OnboardingQuestion]
    offering_schema: OfferingSchema
    calendar_weights: dict[str, float] = {}
    playbook: Playbook
    guardrails: Guardrails

    def event_weight(self, event_name: str) -> float:
        return self.calendar_weights.get(event_name.strip().lower(), 1.0)


class PackLoadError(Exception):
    pass


_REF_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def load_pack(ref: str) -> VerticalPack:
    if not _REF_PATTERN.match(ref):
        raise PackLoadError(f"invalid pack ref: {ref!r}")
    try:
        module = importlib.import_module(f"localpulse.packs.{ref}")
    except ImportError as exc:
        raise PackLoadError(f"no vertical pack named {ref!r}") from exc
    pack = getattr(module, "PACK", None)
    if not isinstance(pack, VerticalPack):
        raise PackLoadError(f"pack {ref!r} does not export a valid PACK")
    return pack
