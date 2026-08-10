"""Engagement Agent (spec §5.4) — handles WhatsApp customer conversations.

A0 auto-answers (FAQs, simple pre-orders) are deterministic pack templates filled
from the Client Context, sent free inside the 24h service window — the model is
never asked to improvise a customer-facing answer, so it can never guess one.
Anything not confidently matched escalates to the owner (A2). The only generated
text is the weekly offer broadcast, which is A1: drafted, guardrail-checked, and
queued for owner approval; the Cost Guard prices it as marketing at publish time.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import asdict, dataclass

from localpulse.agents.common import check_text_guardrails, find_unstocked_item, prices_in
from localpulse.context.models import (
    ApprovalState,
    ClientContext,
    DraftItem,
    DraftKind,
    Offering,
    TemplateSlot,
)
from localpulse.context.repositories import ConversationRepository, EnquiryRepository
from localpulse.llm.gateway import ModelGateway
from localpulse.orchestrator.approval import ApprovalStateMachine
from localpulse.orchestrator.cost_guard import CostGuard, MessagePurpose
from localpulse.orchestrator.messaging import notify_owner, send_whatsapp
from localpulse.orchestrator.templates import TemplateRenderError, render
from localpulse.orchestrator.tool_registry import ToolRegistry
from localpulse.packs.base import VerticalPack, load_pack

logger = logging.getLogger(__name__)

# Platform-level consent keywords (Meta convention) — not vertical, so engine code.
OPT_OUT_KEYWORDS = {"stop", "unsubscribe", "opt out", "opt-out"}
OPT_IN_KEYWORDS = {"start", "unstop", "subscribe", "opt in", "opt-in"}

# Safe fallbacks if a pack leaves a template empty or a template fails to fill.
DEFAULT_ESCALATION_ACK = "Thanks for your message! The owner will get back to you here shortly."
DEFAULT_OPT_OUT_ACK = "Done — you won't receive offers from us anymore."
DEFAULT_OPT_IN_ACK = "You're on the list — we'll send you our offers. Reply STOP anytime to leave."


@dataclass
class InboundResult:
    action: str  # faq | preorder | escalated | opt_out
    reply: str  # what the customer was sent


class EngagementAgent:
    task_profile = "engagement"

    def __init__(
        self,
        gateway: ModelGateway,
        registry: ToolRegistry,
        state_machine: ApprovalStateMachine,
        cost_guard: CostGuard,
        conversations: ConversationRepository,
        enquiries: EnquiryRepository,
        implied_opt_in: bool = False,
    ):
        self._gateway = gateway
        self._registry = registry
        self._state_machine = state_machine
        self._cost_guard = cost_guard
        self._conversations = conversations
        self._enquiries = enquiries
        self._implied_opt_in = implied_opt_in

    # ------------------------------------------------------------------ inbound

    def handle_inbound(
        self, ctx: ClientContext, customer_number: str, text: str, customer_name: str = ""
    ) -> InboundResult:
        """Answer a customer WhatsApp message (A0) or escalate it to the owner (A2)."""
        pack = load_pack(ctx.vertical_pack_ref)
        eng = pack.playbook.engagement
        _, first_contact = self._conversations.upsert_inbound(
            customer_number, customer_name, implied_opt_in=self._implied_opt_in
        )
        lowered = text.lower().strip()
        mapping = self._context_mapping(ctx, customer_name)

        if lowered.strip(" .!") in OPT_OUT_KEYWORDS:
            self._conversations.opt_out(customer_number)
            reply = eng.opt_out_ack or DEFAULT_OPT_OUT_ACK
            return self._reply(ctx, customer_number, text, "opt_out", reply)

        if lowered.strip(" .!") in OPT_IN_KEYWORDS:
            self._conversations.opt_in(customer_number, source="keyword")
            reply = self._fill(eng.opt_in_ack, mapping) or DEFAULT_OPT_IN_ACK
            return self._reply(ctx, customer_number, text, "opt_in", reply)

        # Marketing consent is asked for once, on a first contact, inside the free
        # window — never re-asked, and never on an opt-out (both return above).
        invite = ""
        if first_contact and not self._implied_opt_in:
            invite = self._fill(eng.opt_in_invite, mapping) or ""

        if any(cue in lowered for cue in eng.preorder_cues):
            offering = self._match_offering(ctx, eng.vague_terms, lowered)
            if offering is not None:
                mapping["offering_name"] = offering.name
                mapping["price"] = (
                    f"₹{offering.price_inr:g}" if offering.price_inr else "priced on request"
                )
                reply = self._fill(eng.preorder_ack, mapping)
                if reply is not None:
                    self._notify_owner(
                        ctx,
                        f"🛎 Pre-order enquiry from {customer_name or customer_number} "
                        f'({customer_number}): "{text}"\n'
                        f"I acknowledged and quoted {offering.name} at {mapping['price']} — "
                        "please confirm the order with them.",
                        summary=f"a pre-order enquiry from {customer_name or customer_number}",
                    )
                    return self._reply(ctx, customer_number, text, "preorder", reply, invite)

        for faq in eng.faqs:
            if any(pattern in lowered for pattern in faq.patterns):
                reply = self._fill(faq.answer, mapping)
                if reply is not None:
                    return self._reply(ctx, customer_number, text, "faq", reply, invite)
                logger.warning(
                    "[engagement:%s] FAQ %r template failed to fill — escalating",
                    ctx.client_id,
                    faq.id,
                )
                break  # fall through to escalation rather than trying weaker matches

        # A2: not confidently handled — never a guessed reply (P2 DoD).
        self._notify_owner(
            ctx,
            f"💬 {customer_name or customer_number} ({customer_number}) asked: "
            f'"{text}"\nI didn\'t want to guess — please reply to them directly.',
            summary=f"a question from {customer_name or customer_number} that needs your reply",
        )
        reply = self._fill(eng.escalation_ack, mapping) or DEFAULT_ESCALATION_ACK
        return self._reply(ctx, customer_number, text, "escalated", reply, invite)

    # ---------------------------------------------------------------- broadcast

    def draft_weekly_broadcast(self, ctx: ClientContext, offer_text: str = "") -> DraftItem | None:
        """Draft the weekly offer broadcast (A1). Marketing category — the owner
        approves it and the Cost Guard budget-checks every send at publish time.

        A broadcast lands outside the 24h window, so it goes out as the pack's
        approved marketing template: the model writes only the offer line that fills
        it, and the rendered text the owner approves is byte-for-byte what every
        recipient receives.
        """
        pack = load_pack(ctx.vertical_pack_ref)
        template = pack.message_template(TemplateSlot.WEEKLY_OFFER)
        if template is None:
            logger.warning(
                "[engagement:%s] pack %r has no weekly-offer template — cannot broadcast",
                ctx.client_id,
                pack.ref,
            )
            return None
        recipients = self._conversations.opted_in_numbers()
        if not recipients:
            logger.info("[engagement:%s] no opted-in audience — skipping broadcast", ctx.client_id)
            return None

        offer = offer_text.strip() or self._default_offer(ctx)
        if not offer:
            logger.info("[engagement:%s] nothing to offer — skipping broadcast", ctx.client_id)
            return None
        prompt = "\n".join(
            [
                f"business: {ctx.business.name}",
                f"city: {ctx.business.city}",
                f"offer: {offer}",
                pack.playbook.engagement.broadcast_prompt
                or "Name this week's offer in one short phrase.",
            ]
        )
        system = (
            f"You write the weekly offer line for {ctx.business.name} in "
            f"{ctx.business.city}. Tone: {', '.join(ctx.brand_voice.tone) or 'warm'}. "
            "One short phrase that drops into a fixed message — no greeting, no "
            "sign-off, no links, no health claims."
        )
        # A discount the owner asked for ("₹50 off all cakes") is not on the menu but
        # is authorised by them naming it; a price the model adds on top is not.
        offer_line = self._complete_with_guardrails(
            ctx, pack, prompt, system, extra_prices=prices_in(offer)
        )
        if offer_line is None:
            return None
        try:
            rendered = render(template, {**self._context_mapping(ctx, ""), "offer": offer_line})
        except TemplateRenderError as exc:
            logger.warning("[engagement:%s] broadcast template not filled: %s", ctx.client_id, exc)
            return None
        draft = DraftItem(
            client_id=ctx.client_id,
            kind=DraftKind.WHATSAPP_BROADCAST,
            caption=rendered.body,
            language=ctx.brand_voice.languages[0],
            time_sensitive=True,
            state=ApprovalState.DRAFTED,
            meta={
                "recipients": recipients,
                "offer": offer,
                "template_slot": TemplateSlot.WEEKLY_OFFER.value,
                "template": asdict(rendered),  # approved wording travels with the draft
            },
        )
        return self._state_machine.submit(draft, actor="engagement_agent")

    # ------------------------------------------------------------------ helpers

    def _reply(
        self,
        ctx: ClientContext,
        customer_number: str,
        text: str,
        action: str,
        reply: str,
        invite: str = "",
    ) -> InboundResult:
        body = f"{reply}\n\n{invite}" if invite else reply
        self._enquiries.record(customer_number, text, action)
        send_whatsapp(
            guard=self._cost_guard,
            tool=self._registry.get(ctx.client_id, "whatsapp"),
            to=customer_number,
            body=body,
            purpose=MessagePurpose.REPLY,
            # the inbound message we're answering just (re)opened the free window
            within_service_window=self._conversations.window_open(customer_number),
        )
        return InboundResult(action=action, reply=body)

    def _notify_owner(self, ctx: ClientContext, body: str, summary: str = "") -> None:
        if not self._registry.is_connected(ctx.client_id, "whatsapp"):
            return
        notify_owner(
            guard=self._cost_guard,
            tool=self._registry.get(ctx.client_id, "whatsapp"),
            ctx=ctx,
            pack=load_pack(ctx.vertical_pack_ref),
            body=body,
            purpose=MessagePurpose.NOTIFICATION,
            window_open=self._conversations.window_open(ctx.business.owner_whatsapp),
            summary=summary,
        )

    @staticmethod
    def _match_offering(
        ctx: ClientContext, vague_terms: list[str], lowered: str
    ) -> Offering | None:
        """Identify exactly one offering from the message, or None if not confident."""
        partial: list[Offering] = []
        for offering in ctx.offerings:
            name = offering.name.lower()
            if name in lowered:
                return offering  # full name mentioned — unambiguous
            tokens = [t for t in name.split() if len(t) >= 4 and t not in vague_terms]
            if any(token in lowered for token in tokens):
                partial.append(offering)
        return partial[0] if len(partial) == 1 else None

    @staticmethod
    def _context_mapping(ctx: ClientContext, customer_name: str) -> dict[str, str]:
        hours = ctx.business.hours
        menu = ", ".join(
            f"{o.name} ₹{o.price_inr:g}" if o.price_inr else o.name for o in ctx.offerings
        )
        return {
            "business_name": ctx.business.name,
            "city": ctx.business.city,
            "address": ctx.business.address,
            "phone": ctx.business.phone,
            "hours": hours.get("daily") or ", ".join(f"{d} {h}" for d, h in hours.items()),
            "menu": menu,
            "customer_name": customer_name or "there",
        }

    def _fill(self, template: str, mapping: dict[str, str]) -> str | None:
        """Fill a pack template; a missing placeholder means we can't answer safely."""
        if not template.strip():
            return None
        try:
            return template.format_map(mapping)
        except (KeyError, IndexError, ValueError):
            logger.warning("pack template failed to fill: %r", template)
            return None

    @staticmethod
    def _default_offer(ctx: ClientContext) -> str:
        if not ctx.offerings:
            return ""
        offering = ctx.offerings[0]
        price = f" ₹{offering.price_inr:g}" if offering.price_inr else ""
        return f"{offering.name}{price}"

    def _complete_with_guardrails(
        self,
        ctx: ClientContext,
        pack: VerticalPack,
        prompt: str,
        system: str,
        extra_prices: Collection[float] = (),
    ) -> str | None:
        """The generation loop for the weekly broadcast — its only caller. The item
        check belongs here and not in `check_text_guardrails`, which review replies
        also use: a reply may legitimately echo a reviewer asking for something the
        shop doesn't stock, and dropping that reply would be the wrong call."""
        for attempt in range(2):
            body = self._gateway.complete(self.task_profile, prompt, system=system).strip()
            reason = check_text_guardrails(body, pack, ctx, extra_prices=extra_prices)
            if reason is None:
                invented = find_unstocked_item(body, pack, ctx)
                if invented is not None:
                    reason = f"mentions {invented}, which the shop does not sell"
            if reason is None:
                return body
            logger.info(
                "[engagement:%s] broadcast draft rejected (%s), attempt %d",
                ctx.client_id,
                reason,
                attempt + 1,
            )
            prompt += f"\nThe previous draft was rejected because: {reason}. Fix that."
        return None
