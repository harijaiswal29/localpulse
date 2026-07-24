"""Client Context — the shared per-client data model every agent reads (spec §8).

Agents are stateless: they take (client_context, trigger_payload) and return either
a PublishedAction or a DraftItem that enters the approval queue.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class ApprovalState(StrEnum):
    """Lifecycle of any A1/A2 item. Nothing publishes outside this path."""

    DRAFTED = "drafted"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    PUBLISHED = "published"
    DISCARDED = "discarded"
    EXPIRED = "expired"


class Channel(StrEnum):
    GBP = "gbp"
    WHATSAPP = "whatsapp"
    META = "meta"  # deferred to a later phase


class OfferingType(StrEnum):
    PRODUCT = "product"
    SERVICE = "service"
    APPOINTMENT = "appointment"


class Offering(BaseModel):
    """Polymorphic offering — one model spanning all business types.

    The active type(s) are constrained by the Vertical Pack's offering schema.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str
    type: OfferingType = OfferingType.PRODUCT
    price_inr: float | None = None
    description: str = ""
    # product fields
    variants: list[str] = []
    in_stock: bool | None = None
    # service / appointment fields
    duration_min: int | None = None
    requires_appointment: bool = False


class BusinessProfile(BaseModel):
    name: str
    category: str
    address: str = ""
    city: str = ""
    hours: dict[str, str] = {}
    phone: str = ""
    owner_whatsapp: str = ""


class BrandVoice(BaseModel):
    tone: list[str] = []
    languages: list[str] = ["en"]
    example_posts: list[str] = []


class CalendarEvent(BaseModel):
    name: str
    date: date
    weight: float = 1.0
    hooks: list[str] = []


class ChannelStatus(BaseModel):
    channel: Channel
    connected: bool = False
    credentials_ref: str | None = None  # encrypted credentials live in the DB, not here


class DraftKind(StrEnum):
    GBP_POST = "gbp_post"
    REVIEW_REPLY = "review_reply"
    REVIEW_NUDGE = "review_nudge"  # post-purchase review solicitation (spec §5.3)
    WHATSAPP_BROADCAST = "whatsapp_broadcast"


class TemplateSlot(StrEnum):
    """The out-of-window message slots the engine can send. The engine owns the slot
    (where it is sent from, and what WhatsApp charges for it); the pack owns the wording."""

    REVIEW_NUDGE = "review_nudge"  # post-purchase "please review us"
    WEEKLY_OFFER = "weekly_offer"  # marketing broadcast to the opted-in audience
    OWNER_ALERT = "owner_alert"  # re-opens a cold owner window so detail can follow


# What the engine can fill for each slot. A pack's wording may use any subset of
# these placeholders and nothing else — anything else has no value to fill it with.
SLOT_PARAMS: dict[TemplateSlot, set[str]] = {
    TemplateSlot.REVIEW_NUDGE: {"business_name", "customer_name", "city"},
    TemplateSlot.WEEKLY_OFFER: {"business_name", "offer", "city"},
    TemplateSlot.OWNER_ALERT: {"business_name", "summary"},
}

# Meta's rules for template names and bodies, enforced at pack load so a bad
# template fails here rather than at submission time (or at 2am on a real send).
_TEMPLATE_NAME = re.compile(r"^[a-z][a-z0-9_]{2,60}$")
_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
_ADJACENT_PLACEHOLDERS = re.compile(r"\}\s*\{")
_STOP_KEYWORD = re.compile(r"\bSTOP\b")


class MessageTemplate(BaseModel):
    """A WhatsApp message template: fixed wording, pre-approved by Meta, with named
    placeholders the engine fills at send time.

    Outside the 24h service window WhatsApp delivers nothing else, so every paid
    send resolves to one of these. The wording is a vertical judgement (it lives in
    the pack); which slots exist and what each one costs is engine.
    """

    slot: TemplateSlot
    name: str  # the name registered with Meta, e.g. "bakery_review_nudge_v1"
    body: str  # fixed text with {named} placeholders
    language: str = "en"  # Meta language code

    @property
    def params(self) -> list[str]:
        """Placeholder names in body order — Meta templates take positional params."""
        return _PLACEHOLDER.findall(self.body)

    @model_validator(mode="after")
    def _check_meta_rules(self) -> MessageTemplate:
        if not _TEMPLATE_NAME.match(self.name):
            raise ValueError(f"template name {self.name!r} must be lowercase snake_case")
        body = self.body.strip()
        if not body:
            raise ValueError(f"template {self.name!r} has an empty body")
        params = self.params
        if len(params) != len(set(params)):
            raise ValueError(f"template {self.name!r} repeats a placeholder")
        unknown = set(params) - SLOT_PARAMS[self.slot]
        if unknown:
            raise ValueError(
                f"template {self.name!r} uses placeholders the engine cannot fill for "
                f"the {self.slot.value} slot: {', '.join(sorted(unknown))}"
            )
        # Meta rejects bodies that open or close on a variable, or chain two together:
        # the approved part of a template has to be the fixed text, not the parameters.
        if _PLACEHOLDER.match(body) or body.endswith("}"):
            raise ValueError(f"template {self.name!r} must not start or end with a placeholder")
        if _ADJACENT_PLACEHOLDERS.search(body):
            raise ValueError(f"template {self.name!r} has two adjacent placeholders")
        if self.slot is TemplateSlot.WEEKLY_OFFER and not _STOP_KEYWORD.search(body):
            # The opt-out lives inside the approved body: a template send is fixed
            # text, so the engine can no longer append a footer to it.
            raise ValueError(f"marketing template {self.name!r} must tell the reader to reply STOP")
        return self


class ApprovalPreferences(BaseModel):
    # Draft kinds the owner trusts to publish without a per-item tap (A1 → A0).
    # A2-escalated drafts are never eligible, whatever this list says — that
    # exclusion is enforced in the Approval State Machine, not here.
    auto_publish_kinds: list[DraftKind] = []
    quiet_hours: tuple[int, int] = (21, 8)


class ClientContext(BaseModel):
    """The per-client object every agent reads. All operations scope by client_id."""

    client_id: str
    vertical_pack_ref: str
    business: BusinessProfile
    brand_voice: BrandVoice = BrandVoice()
    offerings: list[Offering] = []
    calendar: list[CalendarEvent] = []
    channels: list[ChannelStatus] = []
    approval_prefs: ApprovalPreferences = ApprovalPreferences()
    subscription_tier: str = "pilot"
    notes: dict[str, str] = {}

    def connected_channels(self) -> set[Channel]:
        return {c.channel for c in self.channels if c.connected}

    def offering_by_name(self, name: str) -> Offering | None:
        lowered = name.strip().lower()
        for offering in self.offerings:
            if offering.name.lower() == lowered:
                return offering
        return None


class DraftItem(BaseModel):
    """An item awaiting owner approval in the content queue."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    client_id: str
    kind: DraftKind
    caption: str
    image_ref: str | None = None
    language: str = "en"
    scheduled_for: date | None = None
    expires_at: datetime | None = None
    time_sensitive: bool = False  # dated items expire on timeout; evergreen re-notify
    state: ApprovalState = ApprovalState.DRAFTED
    meta: dict = {}

    @property
    def short_id(self) -> str:
        return self.id[:8]


class PublishedAction(BaseModel):
    """Record of a publish, always traceable to the approval that authorised it."""

    draft_id: str
    client_id: str
    channel: Channel
    external_ref: str
    approval_log_id: int
    published_at: datetime
