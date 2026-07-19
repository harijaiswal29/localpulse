"""Content Agent (A1) — the workhorse of the MVP. Generates a week of drafts
(caption + image) into the Content Queue using brand voice, the festival
calendar, offerings, and pack templates. Everything it emits enters the
approval queue; nothing goes public from here."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from pydantic import BaseModel

from localpulse.context.models import (
    ApprovalState,
    CalendarEvent,
    ClientContext,
    DraftItem,
    DraftKind,
    Offering,
)
from localpulse.llm.gateway import ModelGateway
from localpulse.orchestrator.approval import ApprovalStateMachine
from localpulse.orchestrator.cost_guard import CostGuard, MessagePurpose
from localpulse.orchestrator.messaging import send_whatsapp
from localpulse.orchestrator.tool_registry import ToolRegistry
from localpulse.packs.base import ContentTemplate, VerticalPack, load_pack

logger = logging.getLogger(__name__)


class ContentTrigger(BaseModel):
    """Trigger payload for a weekly content run."""

    week_start: date


@dataclass
class Slot:
    post_date: date
    template: ContentTemplate
    offering: Offering | None
    event: CalendarEvent | None


def plan_week(pack: VerticalPack, ctx: ClientContext, week_start: date) -> list[Slot]:
    """Pick post dates from the pack playbook and bind each to a template: festival
    templates when a weighted event falls in/near the week, offering rotation otherwise."""
    week_end = week_start + timedelta(days=7)
    events = sorted(
        (e for e in ctx.calendar if week_start <= e.date < week_end + timedelta(days=3)),
        key=lambda e: e.weight * pack.event_weight(e.name),
        reverse=True,
    )
    post_dates = sorted(
        week_start + timedelta(days=(weekday - week_start.isoweekday()) % 7)
        for weekday in pack.playbook.post_weekdays[: pack.playbook.posts_per_week]
    )

    festival_template = next((t for t in pack.templates if t.occasion == "festival"), None)
    rotation = [t for t in pack.templates if t.occasion is None]
    offerings = ctx.offerings

    slots: list[Slot] = []
    used_events: set[str] = set()
    for index, post_date in enumerate(post_dates):
        event = next(
            (
                e
                for e in events
                if e.name not in used_events
                # post just before the festival, or during it (many run multiple days)
                and post_date - timedelta(days=2) <= e.date <= post_date + timedelta(days=4)
            ),
            None,
        )
        if event is not None and festival_template is not None:
            used_events.add(event.name)
            offering = offerings[index % len(offerings)] if offerings else None
            slots.append(Slot(post_date, festival_template, offering, event))
            continue
        template = rotation[index % len(rotation)] if rotation else pack.templates[0]
        offering = None
        if template.requires_offering:
            if not offerings:
                continue  # fail closed: no grounded offering available for this template
            offering = offerings[index % len(offerings)]
        slots.append(Slot(post_date, template, offering, None))
    return slots


def check_guardrails(caption: str, slot: Slot, pack: VerticalPack) -> str | None:
    """Return a rejection reason, or None if the caption is safe to show the owner."""
    if not caption.strip():
        return "empty caption"
    if len(caption) > pack.guardrails.max_caption_chars:
        return "caption too long"
    lowered = caption.lower()
    for term in pack.guardrails.banned_terms:
        if term.lower() in lowered:
            return f"banned term: {term}"
    if (
        pack.guardrails.require_offering_grounding
        and slot.template.requires_offering
        and slot.offering is not None
        and slot.offering.name.lower() not in lowered
    ):
        return "caption not grounded in the selected offering"
    return None


class ContentAgent:
    task_profile = "content"

    def __init__(
        self,
        gateway: ModelGateway,
        registry: ToolRegistry,
        state_machine: ApprovalStateMachine,
        cost_guard: CostGuard,
    ):
        self._gateway = gateway
        self._registry = registry
        self._state_machine = state_machine
        self._cost_guard = cost_guard

    def run(self, ctx: ClientContext, trigger: ContentTrigger) -> list[DraftItem]:
        pack = load_pack(ctx.vertical_pack_ref)
        drafts: list[DraftItem] = []
        for slot in plan_week(pack, ctx, trigger.week_start):
            caption = self._generate_caption(ctx, pack, slot)
            if caption is None:
                logger.warning(
                    "[content:%s] skipping slot %s/%s — validation failed twice",
                    ctx.client_id,
                    slot.post_date,
                    slot.template.id,
                )
                continue  # malformed generation: retried once, else skip the slot (§12.1)
            drafts.append(self._enqueue(ctx, slot, caption))
        self._notify_owner(ctx, drafts)
        return drafts

    def _generate_caption(self, ctx: ClientContext, pack: VerticalPack, slot: Slot) -> str | None:
        system = (
            f"You write short Google Business Profile captions for {ctx.business.name}, "
            f"a {ctx.business.category.lower()} in {ctx.business.city}. "
            f"Tone: {', '.join(ctx.brand_voice.tone) or 'warm'}. "
            "Only mention items listed in the prompt — never invent offerings, prices, "
            "or health claims."
        )
        prompt = self._prompt_for(ctx, pack, slot)
        for attempt in range(2):
            caption = self._gateway.complete(self.task_profile, prompt, system=system).strip()
            reason = check_guardrails(caption, slot, pack)
            if reason is None:
                return caption
            logger.info(
                "[content:%s] draft rejected (%s), attempt %d", ctx.client_id, reason, attempt + 1
            )
            prompt += f"\nThe previous draft was rejected because: {reason}. Fix that."
        return None

    def _prompt_for(self, ctx: ClientContext, pack: VerticalPack, slot: Slot) -> str:
        lines = [
            f"business: {ctx.business.name}",
            f"hook: {slot.template.hook}",
            f"language: {ctx.brand_voice.languages[0]}",
            f"date: {slot.post_date.isoformat()}",
        ]
        if slot.offering is not None:
            price = f" (₹{slot.offering.price_inr:.0f})" if slot.offering.price_inr else ""
            lines.append(f"offering: {slot.offering.name}{price}")
        if slot.event is not None:
            lines.append(f"occasion: {slot.event.name}")
            if slot.event.hooks:
                lines.append(f"occasion_hooks: {', '.join(slot.event.hooks)}")
        lines.append(slot.template.prompt)
        lines.append(f"Keep it under {pack.guardrails.max_caption_chars} characters.")
        return "\n".join(lines)

    def _enqueue(self, ctx: ClientContext, slot: Slot, caption: str) -> DraftItem:
        image_ref = None
        if self._registry.is_connected(ctx.client_id, "imagegen"):
            image_prompt = (
                f"Appealing photo-style image for a {ctx.business.category.lower()}: "
                f"{slot.offering.name if slot.offering else slot.template.hook}"
                + (f", {slot.event.name} theme" if slot.event else "")
            )
            try:
                image_ref = self._registry.get(ctx.client_id, "imagegen").generate(image_prompt)
            except Exception:  # degrade gracefully: a text-only draft beats none (§12.1)
                logger.exception("[content:%s] image generation failed", ctx.client_id)

        time_sensitive = slot.event is not None
        expires_at = None
        if time_sensitive and slot.event is not None:
            expires_at = datetime.combine(
                max(slot.event.date, slot.post_date), time(23, 59), tzinfo=UTC
            )

        draft = DraftItem(
            client_id=ctx.client_id,
            kind=DraftKind.GBP_POST,
            caption=caption,
            image_ref=image_ref,
            language=ctx.brand_voice.languages[0],
            scheduled_for=slot.post_date,
            expires_at=expires_at,
            time_sensitive=time_sensitive,
            state=ApprovalState.DRAFTED,
            meta={
                "template_id": slot.template.id,
                "event": slot.event.name if slot.event else None,
            },
        )
        return self._state_machine.submit(draft, actor="content_agent")

    def _notify_owner(self, ctx: ClientContext, drafts: list[DraftItem]) -> None:
        if not drafts or not self._registry.is_connected(ctx.client_id, "whatsapp"):
            return
        pending = [d for d in drafts if d.state == ApprovalState.PENDING_APPROVAL]
        auto = [d for d in drafts if d not in pending]  # approved by standing preference
        lines: list[str] = []
        if auto:
            lines.append(f"🚀 {len(auto)} post(s) publishing automatically (your AUTO setting):")
            for draft in auto:
                lines.append(f"\n[{draft.short_id}] {draft.scheduled_for}: {draft.caption}")
        if pending:
            if lines:
                lines.append("")
            lines.append(f"🗓 {len(pending)} draft post(s) ready for your review:")
            for draft in pending:
                lines.append(f"\n[{draft.short_id}] {draft.scheduled_for}: {draft.caption}")
            lines.append("\nReply APPROVE <id>, EDIT <id> <new text>, or SKIP <id> for each.")
        send_whatsapp(
            guard=self._cost_guard,
            tool=self._registry.get(ctx.client_id, "whatsapp"),
            to=ctx.business.owner_whatsapp,
            body="\n".join(lines),
            purpose=MessagePurpose.APPROVAL_REQUEST,
            within_service_window=True,  # owner chat stays warm; BSP window state later
        )
