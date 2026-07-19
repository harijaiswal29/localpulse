"""Task Router — dispatches cadence tasks to the right agent for a client.

Task names are strings from the pack playbook, so packs stay in control of the
rhythm and new behaviours (e.g. future nurture sequences) slot in without
touching the engine (spec §2.2, §6).

Multi-tenant hardening: one client's failure must never starve the others.
Every dispatch is isolated — exceptions are contained per client/task, and a
task that keeps failing trips a circuit breaker so the worker stops burning
its cycles (and the client's budget) on it until the cooldown passes."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from localpulse.agents.content import ContentTrigger
from localpulse.container import ClientServices, Container
from localpulse.context.repositories import NotFoundError
from localpulse.orchestrator.cost_guard import MessagePurpose
from localpulse.orchestrator.messaging import send_whatsapp

logger = logging.getLogger(__name__)

# Consecutive failures of one client/task before its circuit opens, and how
# long the circuit stays open before the task gets another chance.
FAILURE_THRESHOLD = 3
CIRCUIT_COOLDOWN = timedelta(minutes=30)


def next_week_start(today: date | None = None) -> date:
    today = today or datetime.now(UTC).date()
    return today + timedelta(days=(8 - today.isoweekday()) % 7 or 7)


class TaskRouter:
    def __init__(self, container: Container):
        self._container = container
        self._failure_streak: dict[tuple[str, str], int] = {}
        self._circuit_open_until: dict[tuple[str, str], datetime] = {}

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def dispatch(self, client_id: str, task: str) -> bool:
        """Run one cadence task for one client. Returns True if the task ran.

        Never raises: a failing client is logged and circuit-broken, so the
        scheduler thread and every other client's cadence stay unaffected.
        """
        key = (client_id, task)
        open_until = self._circuit_open_until.get(key)
        if open_until is not None:
            if self._now() < open_until:
                logger.warning(
                    "circuit open for %s:%s until %s — skipping run", client_id, task, open_until
                )
                return False
            del self._circuit_open_until[key]  # cooldown over: give it another chance

        logger.info("dispatch task=%s client=%s", task, client_id)
        try:
            with self._container.session() as session:
                services = self._container.services(session, client_id)
                self._execute(services, client_id, task)
        except NotFoundError:
            logger.warning(
                "client %s no longer exists — skipping %s; the job will be "
                "dropped at the next schedule resync",
                client_id,
                task,
            )
            return False
        except Exception:
            streak = self._failure_streak.get(key, 0) + 1
            self._failure_streak[key] = streak
            logger.exception(
                "task %s failed for client %s (failure %d/%d)",
                task,
                client_id,
                streak,
                FAILURE_THRESHOLD,
            )
            if streak >= FAILURE_THRESHOLD:
                until = self._now() + CIRCUIT_COOLDOWN
                self._circuit_open_until[key] = until
                self._failure_streak.pop(key, None)
                logger.error(
                    "circuit opened for %s:%s until %s after %d consecutive failures",
                    client_id,
                    task,
                    until,
                    FAILURE_THRESHOLD,
                )
            return False
        self._failure_streak.pop(key, None)
        return True

    def _execute(self, services: ClientServices, client_id: str, task: str) -> None:
        ctx = services.context
        if task == "content.generate_week":
            services.content_agent.run(ctx, ContentTrigger(week_start=next_week_start()))
        elif task == "reputation.check_reviews":
            drafted = services.reputation_agent.check_reviews(ctx)
            if drafted:
                logger.info("drafted %d review reply draft(s) for %s", len(drafted), client_id)
        elif task == "engagement.weekly_broadcast":
            draft = services.engagement_agent.draft_weekly_broadcast(ctx)
            if draft:
                logger.info(
                    "drafted weekly broadcast %s for %s (%d recipient(s))",
                    draft.short_id,
                    client_id,
                    len(draft.meta.get("recipients", [])),
                )
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
