"""Task Router — dispatches cadence tasks to the right agent for a client.

Task names are strings from the pack playbook, so packs stay in control of the
rhythm and new behaviours (e.g. future nurture sequences) slot in without
touching the engine (spec §2.2, §6)."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from localpulse.agents.content import ContentTrigger
from localpulse.container import Container
from localpulse.orchestrator.cost_guard import MessagePurpose
from localpulse.orchestrator.messaging import send_whatsapp

logger = logging.getLogger(__name__)


def next_week_start(today: date | None = None) -> date:
    today = today or datetime.now(UTC).date()
    return today + timedelta(days=(8 - today.isoweekday()) % 7 or 7)


class TaskRouter:
    def __init__(self, container: Container):
        self._container = container

    def dispatch(self, client_id: str, task: str) -> None:
        logger.info("dispatch task=%s client=%s", task, client_id)
        with self._container.session() as session:
            services = self._container.services(session, client_id)
            ctx = services.context
            if task == "content.generate_week":
                services.content_agent.run(ctx, ContentTrigger(week_start=next_week_start()))
            elif task == "reputation.check_reviews":
                drafted = services.reputation_agent.check_reviews(ctx)
                if drafted:
                    logger.info("drafted %d review reply draft(s) for %s", len(drafted), client_id)
            elif task == "insights.collect":
                services.insights_agent.collect_daily(ctx)
            elif task == "insights.monthly_report":
                now = datetime.now(UTC)
                previous = now.replace(day=1) - timedelta(days=1)
                report = services.insights_agent.monthly_report(ctx, previous.year, previous.month)
                send_whatsapp(
                    guard=services.cost_guard,
                    tool=self._container.registry.get(client_id, "whatsapp"),
                    to=ctx.business.owner_whatsapp,
                    body=report,
                    purpose=MessagePurpose.NOTIFICATION,
                    within_service_window=False,
                )
            elif task == "approvals.sweep_expired":
                expired = services.state_machine.sweep_expired()
                if expired:
                    logger.info("expired %d pending item(s) for %s", len(expired), client_id)
            else:
                logger.warning("unknown task %r for client %s — skipping", task, client_id)
