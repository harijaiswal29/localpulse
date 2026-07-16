"""FastAPI app: WhatsApp inbound webhook (owner Approve/Edit/Skip) + approval and
onboarding endpoints. Owner interaction happens entirely in WhatsApp (spec §9)."""

from __future__ import annotations

from datetime import date

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from localpulse.agents.content import ContentTrigger
from localpulse.config import Settings
from localpulse.container import ClientServices, Container
from localpulse.context.models import ApprovalState, DraftItem
from localpulse.context.repositories import ClientRepository, NotFoundError
from localpulse.orchestrator.approval import IllegalTransitionError
from localpulse.orchestrator.cost_guard import BudgetExceededError, MessagePurpose
from localpulse.orchestrator.messaging import send_whatsapp
from localpulse.orchestrator.publisher import publish_draft


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


def _handle_owner_command(services: ClientServices, container: Container, text: str) -> str:
    """Parse an owner WhatsApp message: LIST · APPROVE <id> · EDIT <id> <text> · SKIP <id>."""
    words = text.strip().split(maxsplit=2)
    if not words:
        return "Reply LIST to see drafts, or APPROVE/EDIT/SKIP <id>."
    command = words[0].lower()
    owner = "owner"

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
        ctx = ClientRepository(session).find_by_whatsapp(payload.from_number)
        if ctx is None:
            return {"reply": "This number isn't linked to a LocalPulse client."}
        services = container.services(session, ctx.client_id)
        reply = _handle_owner_command(services, container, payload.text)
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
        return {"drafts": [_preview(d) for d in drafts]}

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
        return {"drafts": [_preview(d) for d in drafts]}

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
        return _preview(draft)

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
