"""Cadence Engine worker: builds per-client schedules from each client's pack
playbook and drives proactive agent runs.

    python -m localpulse.orchestrator.worker
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from localpulse.container import Container
from localpulse.context.repositories import ClientRepository
from localpulse.orchestrator.router import TaskRouter
from localpulse.packs.base import load_pack

logger = logging.getLogger(__name__)


def build_scheduler(container: Container) -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone="Asia/Kolkata")
    router = TaskRouter(container)
    with container.session() as session:
        clients = ClientRepository(session)
        for client_id in clients.list_client_ids():
            ctx = clients.get(client_id)
            pack = load_pack(ctx.vertical_pack_ref)
            for rule in pack.playbook.cadence:
                scheduler.add_job(
                    router.dispatch,
                    CronTrigger.from_crontab(rule.cron),
                    args=[client_id, rule.task],
                    id=f"{client_id}:{rule.task}",
                    replace_existing=True,
                    coalesce=True,  # per-tenant isolation: a stalled run must not pile up
                    max_instances=1,
                )
                logger.info("scheduled %s for %s (%s)", rule.task, client_id, rule.cron)
    return scheduler


def main() -> None:
    logging.basicConfig(level="INFO")
    container = Container()
    scheduler = build_scheduler(container)
    logger.info("cadence engine running — Ctrl+C to stop")
    scheduler.start()


if __name__ == "__main__":
    main()
