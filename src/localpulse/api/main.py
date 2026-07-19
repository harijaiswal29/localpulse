"""FastAPI app: WhatsApp inbound webhook (owner Approve/Edit/Skip) + approval and
onboarding endpoints. Owner interaction happens entirely in WhatsApp (spec §9)."""

from __future__ import annotations

from datetime import date

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from localpulse.agents.content import ContentTrigger
from localpulse.config import Settings
from localpulse.container import ClientServices, Container
from localpulse.context.models import ApprovalState, DraftItem, DraftKind
from localpulse.context.repositories import ClientRepository, NotFoundError
from localpulse.orchestrator.approval import IllegalTransitionError
from localpulse.orchestrator.cost_guard import BudgetExceededError, MessagePurpose
from localpulse.orchestrator.messaging import send_whatsapp
from localpulse.orchestrator.publisher import publish_draft, publish_ready

_KIND_NAMES = ", ".join(kind.value for kind in DraftKind)


class WhatsAppInbound(BaseModel):
    from_number: str
    text: str


class OnboardRequest(BaseModel):
    pack_ref: str = "bakery"
    answers: dict[str, str]


class ContentRunRequest(BaseModel):
    week_start: date


class NudgeRequest(BaseModel):
    customer_number: str
    customer_name: str = ""
    within_service_window: bool = False


class CustomerInbound(BaseModel):
    customer_number: str
    text: str
    customer_name: str = ""


class BroadcastRequest(BaseModel):
    offer_text: str = ""


class ApprovalPrefsRequest(BaseModel):
    auto_publish_kinds: list[DraftKind]


def _preview(draft: DraftItem) -> dict:
    return {
        "id": draft.id,
        "short_id": draft.short_id,
        "kind": draft.kind.value,
        "state": draft.state.value,
        "caption": draft.caption,
        "image_ref": draft.image_ref,
        "language": draft.language,
        "scheduled_for": draft.scheduled_for.isoformat() if draft.scheduled_for else None,
        "meta": draft.meta,
    }


def _approve_and_publish(services: ClientServices, container: Container, draft_id: str, actor: str):
    draft, approval_log_id = services.state_machine.approve(draft_id, actor=actor)
    return publish_draft(
        draft_id=draft.id,
        approval_log_id=approval_log_id,
        queue=services.queue,
        publish_log=services.publish_log,
        state_machine=services.state_machine,
        registry=container.registry,
        cost_guard=services.cost_guard,
        reviews=services.reviews,
    )


def _handle_auto_command(
    services: ClientServices, clients: ClientRepository, args: list[str]
) -> str:
    """AUTO · AUTO ON <kind> · AUTO OFF <kind> — the owner's A1→A0 promotions."""
    prefs = services.context.approval_prefs
    usage = (
        f"Reply AUTO ON <kind> or AUTO OFF <kind>. Kinds: {_KIND_NAMES}.\n"
        "Anything I escalate to you always waits for your approval."
    )
    if not args:
        current = ", ".join(k.value for k in prefs.auto_publish_kinds) or "nothing"
        return f"Auto-publish is on for: {current}.\n{usage}"
    if args[0].lower() not in {"on", "off"} or len(args) < 2:
        return usage
    try:
        kind = DraftKind(args[1].strip().lower())
    except ValueError:
        return f"'{args[1]}' isn't a draft kind I know. Kinds: {_KIND_NAMES}."
    kinds = set(prefs.auto_publish_kinds)
    if args[0].lower() == "on":
        kinds.add(kind)
        reply = (
            f"✅ From now on I'll publish {kind.value} drafts automatically. "
            "Anything I escalate still waits for you. Reply AUTO OFF "
            f"{kind.value} to turn this off."
        )
    else:
        kinds.discard(kind)
        reply = f"👍 {kind.value} drafts will wait for your approval again."
    prefs.auto_publish_kinds = sorted(kinds)
    clients.save(services.context)
    return reply


def _handle_owner_command(
    services: ClientServices, container: Container, clients: ClientRepository, text: str
) -> str:
    """Parse an owner WhatsApp message: LIST · APPROVE <id> · EDIT <id> <text> ·
    SKIP <id> · AUTO [ON|OFF <kind>]."""
    words = text.strip().split(maxsplit=2)
    if not words:
        return "Reply LIST to see drafts, or APPROVE/EDIT/SKIP <id>."
    command = words[0].lower()
    owner = "owner"

    if command == "auto":
        return _handle_auto_command(services, clients, words[1:])

    if command in {"list", "queue"}:
        pending = services.queue.list(state=ApprovalState.PENDING_APPROVAL)
        if not pending:
            return "No drafts waiting for you. 🎉"
        lines = ["Drafts waiting for approval:"]
        for draft in pending:
            flag = "⚠️ " if draft.meta.get("escalated") else ""
            when = f"{draft.scheduled_for}: " if draft.scheduled_for else ""
            lines.append(f"\n{flag}[{draft.short_id}] {when}{draft.caption}")
        return "\n".join(lines)

    if command in {"approve", "edit", "skip"} and len(words) >= 2:
        draft = services.queue.find_by_prefix(words[1])
        if draft is None:
            return f"Couldn't find a draft starting with '{words[1]}'. Reply LIST to see ids."
        try:
            if command == "approve":
                action = _approve_and_publish(services, container, draft.id, owner)
                return f"✅ Approved and published [{draft.short_id}] ({action.external_ref})."
            if command == "edit":
                if len(words) < 3 or not words[2].strip():
                    return "To edit, send: EDIT <id> <new caption>."
                services.state_machine.edit(draft.id, words[2].strip(), actor=owner)
                return f"✏️ Updated [{draft.short_id}]. Reply APPROVE {draft.short_id} when ready."
            services.state_machine.reject(draft.id, actor=owner, note="skipped via WhatsApp")
            return f"⏭ Skipped [{draft.short_id}]."
        except IllegalTransitionError:
            return f"[{draft.short_id}] is already {draft.state.value} — nothing to do."

    return "Reply LIST to see drafts, or APPROVE/EDIT/SKIP <id>."


def create_app(settings: Settings | None = None) -> FastAPI:
    container = Container(settings)
    app = FastAPI(title="LocalPulse", version="0.1.0")
    app.state.container = container

    def get_session():
        session = container.session()
        try:
            yield session
        finally:
            session.close()

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/webhooks/whatsapp")
    def whatsapp_webhook(payload: WhatsAppInbound, session=Depends(get_session)) -> dict:
        clients = ClientRepository(session)
        ctx = clients.find_by_whatsapp(payload.from_number)
        if ctx is None:
            return {"reply": "This number isn't linked to a LocalPulse client."}
        services = container.services(session, ctx.client_id)
        reply = _handle_owner_command(services, container, clients, payload.text)
        try:
            # inbound message just opened the 24h service window -> free reply
            send_whatsapp(
                guard=services.cost_guard,
                tool=container.registry.get(ctx.client_id, "whatsapp"),
                to=payload.from_number,
                body=reply,
                purpose=MessagePurpose.REPLY,
                within_service_window=True,
            )
        except BudgetExceededError:
            pass  # service replies are free; belt-and-braces only
        return {"reply": reply}

    @app.post("/clients/{client_id}/onboard")
    def onboard(client_id: str, request: OnboardRequest, session=Depends(get_session)) -> dict:
        agent = container.onboarding_agent(session)
        context = agent.run(client_id, request.pack_ref, request.answers)
        container.ensure_client_tools(context)
        return context.model_dump(mode="json")

    @app.post("/clients/{client_id}/content/run")
    def run_content(
        client_id: str, request: ContentRunRequest, session=Depends(get_session)
    ) -> dict:
        services = _services_or_404(container, session, client_id)
        drafts = services.content_agent.run(
            services.context, ContentTrigger(week_start=request.week_start)
        )
        published = publish_ready(services, container.registry)
        if published:  # auto-approved drafts went straight out — show their final state
            drafts = [services.queue.get(d.id) for d in drafts]
        return {"drafts": [_preview(d) for d in drafts], "auto_published": len(published)}

    @app.get("/clients/{client_id}/queue")
    def queue(client_id: str, state: str | None = None, session=Depends(get_session)) -> dict:
        services = _services_or_404(container, session, client_id)
        filter_state = ApprovalState(state) if state else None
        return {"items": [_preview(d) for d in services.queue.list(state=filter_state)]}

    @app.post("/clients/{client_id}/drafts/{draft_id}/approve")
    def approve(client_id: str, draft_id: str, session=Depends(get_session)) -> dict:
        services = _services_or_404(container, session, client_id)
        try:
            action = _approve_and_publish(services, container, draft_id, actor="owner_api")
        except NotFoundError:
            raise HTTPException(404, "draft not found") from None
        except IllegalTransitionError as exc:
            raise HTTPException(409, str(exc)) from None
        return action.model_dump(mode="json")

    @app.post("/clients/{client_id}/reputation/check")
    def check_reviews(client_id: str, session=Depends(get_session)) -> dict:
        services = _services_or_404(container, session, client_id)
        drafts = services.reputation_agent.check_reviews(services.context)
        published = publish_ready(services, container.registry)
        if published:
            drafts = [services.queue.get(d.id) for d in drafts]
        return {"drafts": [_preview(d) for d in drafts], "auto_published": len(published)}

    @app.post("/clients/{client_id}/reputation/nudge")
    def draft_nudge(client_id: str, request: NudgeRequest, session=Depends(get_session)) -> dict:
        services = _services_or_404(container, session, client_id)
        draft = services.reputation_agent.draft_review_nudge(
            services.context,
            customer_number=request.customer_number,
            customer_name=request.customer_name,
            within_service_window=request.within_service_window,
        )
        if draft is None:
            raise HTTPException(422, "could not draft a nudge that passes guardrails")
        if publish_ready(services, container.registry):
            draft = services.queue.get(draft.id)
        return _preview(draft)

    @app.post("/clients/{client_id}/engagement/inbound")
    def customer_inbound(
        client_id: str, payload: CustomerInbound, session=Depends(get_session)
    ) -> dict:
        """Customer WhatsApp message routed to a client (the BSP webhook maps its
        phone_number_id to a client and forwards here). A0 auto-answer or A2 escalate."""
        services = _services_or_404(container, session, client_id)
        result = services.engagement_agent.handle_inbound(
            services.context,
            customer_number=payload.customer_number,
            text=payload.text,
            customer_name=payload.customer_name,
        )
        return {"action": result.action, "reply": result.reply}

    @app.post("/clients/{client_id}/engagement/broadcast")
    def draft_broadcast(
        client_id: str, request: BroadcastRequest, session=Depends(get_session)
    ) -> dict:
        services = _services_or_404(container, session, client_id)
        draft = services.engagement_agent.draft_weekly_broadcast(
            services.context, offer_text=request.offer_text
        )
        if draft is None:
            raise HTTPException(
                422, "could not draft a broadcast (no opted-in audience, or guardrails failed)"
            )
        if publish_ready(services, container.registry):
            draft = services.queue.get(draft.id)
        return _preview(draft)

    @app.put("/clients/{client_id}/approval-preferences")
    def set_approval_prefs(
        client_id: str, request: ApprovalPrefsRequest, session=Depends(get_session)
    ) -> dict:
        """Owner promotes trusted draft kinds from A1 to A0. Escalated (A2) items
        are always excluded — the state machine enforces that, not this endpoint."""
        services = _services_or_404(container, session, client_id)
        ctx = services.context
        ctx.approval_prefs.auto_publish_kinds = sorted(set(request.auto_publish_kinds))
        ClientRepository(session).save(ctx)
        return {"approval_prefs": ctx.approval_prefs.model_dump(mode="json")}

    @app.get("/clients/{client_id}/report/{year}/{month}")
    def report(client_id: str, year: int, month: int, session=Depends(get_session)) -> dict:
        services = _services_or_404(container, session, client_id)
        return {"report": services.insights_agent.monthly_report(services.context, year, month)}

    @app.post("/clients/{client_id}/insights/collect")
    def collect(client_id: str, session=Depends(get_session)) -> dict:
        services = _services_or_404(container, session, client_id)
        return services.insights_agent.collect_daily(services.context)

    return app


def _services_or_404(container: Container, session, client_id: str) -> ClientServices:
    try:
        return container.services(session, client_id)
    except NotFoundError:
        raise HTTPException(404, f"unknown client {client_id!r}") from None


app = create_app()
