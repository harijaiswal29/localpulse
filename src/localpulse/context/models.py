"""Client Context — the shared per-client data model every agent reads (spec §8).

Agents are stateless: they take (client_context, trigger_payload) and return either
a PublishedAction or a DraftItem that enters the approval queue.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


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


class ApprovalPreferences(BaseModel):
    auto_publish: bool = False  # P0: always False — everything public needs approval
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


class DraftKind(StrEnum):
    GBP_POST = "gbp_post"
    REVIEW_REPLY = "review_reply"
    REVIEW_NUDGE = "review_nudge"  # post-purchase review solicitation (spec §5.3)
    WHATSAPP_BROADCAST = "whatsapp_broadcast"


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
