"""Composition root: wires settings, DB, gateway, tool registry, and per-client
services. Shared by the API app and the scheduler worker."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from localpulse.agents.content import ContentAgent
from localpulse.agents.insights import InsightsAgent
from localpulse.agents.onboarding import OnboardingAgent
from localpulse.agents.reputation import ReputationAgent
from localpulse.config import Settings, get_settings
from localpulse.context.models import ClientContext
from localpulse.context.repositories import (
    ApprovalLogRepository,
    ClientRepository,
    ContentQueueRepository,
    CostLedgerRepository,
    MetricsRepository,
    PublishLogRepository,
    ReviewRepository,
)
from localpulse.data.db import init_db, make_engine, make_session_factory
from localpulse.llm.gateway import ModelGateway
from localpulse.orchestrator.approval import ApprovalStateMachine
from localpulse.orchestrator.cost_guard import CostGuard
from localpulse.orchestrator.tool_registry import ToolRegistry
from localpulse.tools.gbp import SemiManualGbpTool
from localpulse.tools.imagegen import MockImageGenTool
from localpulse.tools.whatsapp import MockWhatsAppTool


@dataclass
class ClientServices:
    """Everything needed to act for one client — all scoped to its client_id."""

    context: ClientContext
    queue: ContentQueueRepository
    approval_log: ApprovalLogRepository
    publish_log: PublishLogRepository
    metrics: MetricsRepository
    reviews: ReviewRepository
    state_machine: ApprovalStateMachine
    cost_guard: CostGuard
    content_agent: ContentAgent
    reputation_agent: ReputationAgent
    insights_agent: InsightsAgent


class Container:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.engine = make_engine(self.settings.database_url)
        init_db(self.engine)
        self.session_factory = make_session_factory(self.engine)
        self.gateway = ModelGateway.from_settings(self.settings)
        self.registry = ToolRegistry()

    def session(self) -> Session:
        return self.session_factory()

    def onboarding_agent(self, session: Session) -> OnboardingAgent:
        return OnboardingAgent(ClientRepository(session))

    def ensure_client_tools(self, ctx: ClientContext) -> None:
        """Register default P0 tools for a client's connected channels (idempotent)."""
        client_id = ctx.client_id
        if not self.registry.is_connected(client_id, "gbp"):
            self.registry.register(client_id, "gbp", SemiManualGbpTool(client_id=client_id))
        if not self.registry.is_connected(client_id, "whatsapp"):
            self.registry.register(client_id, "whatsapp", MockWhatsAppTool(client_id=client_id))
        if not self.registry.is_connected(client_id, "imagegen"):
            self.registry.register(client_id, "imagegen", MockImageGenTool(client_id=client_id))

    def services(self, session: Session, client_id: str) -> ClientServices:
        context = ClientRepository(session).get(client_id)
        self.ensure_client_tools(context)
        queue = ContentQueueRepository(session, client_id)
        approval_log = ApprovalLogRepository(session, client_id)
        publish_log = PublishLogRepository(session, client_id)
        metrics = MetricsRepository(session, client_id)
        reviews = ReviewRepository(session, client_id)
        state_machine = ApprovalStateMachine(queue, approval_log)
        cost_guard = CostGuard(
            CostLedgerRepository(session, client_id),
            self.settings.default_monthly_budget_inr,
        )
        return ClientServices(
            context=context,
            queue=queue,
            approval_log=approval_log,
            publish_log=publish_log,
            metrics=metrics,
            reviews=reviews,
            state_machine=state_machine,
            cost_guard=cost_guard,
            content_agent=ContentAgent(self.gateway, self.registry, state_machine, cost_guard),
            reputation_agent=ReputationAgent(
                self.gateway, self.registry, state_machine, cost_guard, reviews
            ),
            insights_agent=InsightsAgent(metrics, publish_log, self.registry, reviews),
        )
