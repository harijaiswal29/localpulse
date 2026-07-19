"""Cadence Engine worker: builds per-client schedules from each client's pack
playbook and drives proactive agent runs.

    python -m localpulse.orchestrator.worker

Multi-tenant hardening: the schedule is not a startup snapshot. A resync job
re-reads the tenant directory on an interval, so clients onboarded while the
worker runs get scheduled, deleted clients get unscheduled, and a pack change
re-schedules — all without a restart. A client whose pack fails to load is
skipped with an error; it never blocks the other clients' cadence.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.base import BaseScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from localpulse.container import Container
from localpulse.context.repositories import ClientRepository
from localpulse.orchestrator.router import TaskRouter
from localpulse.packs.base import load_pack

logger = logging.getLogger(__name__)

RESYNC_JOB_ID = "worker:resync"


def _desired_jobs(container: Container) -> dict[str, tuple[str, str, str]]:
    """job_id -> (client_id, task, cron) for every client in the tenant directory."""
    desired: dict[str, tuple[str, str, str]] = {}
    with container.session() as session:
        clients = ClientRepository(session)
        for client_id in clients.list_client_ids():
            try:
                ctx = clients.get(client_id)
                pack = load_pack(ctx.vertical_pack_ref)
            except Exception:
                logger.exception(
                    "cannot build schedule for client %s — skipping it this resync "
                    "(other clients unaffected)",
                    client_id,
                )
                continue
            for rule in pack.playbook.cadence:
                desired[f"{client_id}:{rule.task}"] = (client_id, rule.task, rule.cron)
    return desired


def sync_schedule(
    scheduler: BaseScheduler, container: Container, router: TaskRouter
) -> tuple[int, int]:
    """Reconcile scheduler jobs with the tenant directory. Returns (changed, removed)."""
    desired = _desired_jobs(container)
    existing = {job.id: job.name for job in scheduler.get_jobs() if job.id != RESYNC_JOB_ID}

    changed = 0
    for job_id, (client_id, task, cron) in desired.items():
        name = f"{task} @ {cron}"
        if existing.get(job_id) == name:
            continue  # already scheduled with the same cadence — leave it alone
        scheduler.add_job(
            router.dispatch,
            CronTrigger.from_crontab(cron),
            args=[client_id, task],
            id=job_id,
            name=name,
            replace_existing=True,
            coalesce=True,  # per-tenant isolation: a stalled run must not pile up
            max_instances=1,
        )
        changed += 1
        logger.info("scheduled %s for %s (%s)", task, client_id, cron)

    removed = 0
    for job_id in existing.keys() - desired.keys():
        scheduler.remove_job(job_id)
        removed += 1
        logger.info("unscheduled stale job %s (client gone or cadence dropped)", job_id)

    if changed or removed:
        logger.info(
            "schedule resync: %d job(s) added/updated, %d removed, %d active",
            changed,
            removed,
            len(desired),
        )
    return changed, removed


def build_scheduler(container: Container, scheduler: BaseScheduler | None = None) -> BaseScheduler:
    scheduler = scheduler or BlockingScheduler(timezone="Asia/Kolkata")
    router = TaskRouter(container)
    scheduler.add_job(
        sync_schedule,
        IntervalTrigger(minutes=container.settings.worker_resync_minutes),
        args=[scheduler, container, router],
        id=RESYNC_JOB_ID,
        name="tenant directory resync",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    sync_schedule(scheduler, container, router)
    return scheduler


def main() -> None:
    logging.basicConfig(level="INFO")
    container = Container()
    scheduler = build_scheduler(container)
    logger.info("cadence engine running — Ctrl+C to stop")
    scheduler.start()


if __name__ == "__main__":
    main()
