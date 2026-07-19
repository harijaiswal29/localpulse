"""P3 multi-client worker hardening tests.

One client's failure must never starve the others: dispatch is isolated per
client/task with a circuit breaker, and the schedule tracks the tenant
directory live (new clients scheduled, deleted clients unscheduled, a broken
pack skipped) instead of being a startup snapshot.
"""

from datetime import UTC, datetime, timedelta

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from localpulse.context.repositories import ClientRepository
from localpulse.orchestrator.router import CIRCUIT_COOLDOWN, FAILURE_THRESHOLD, TaskRouter
from localpulse.orchestrator.worker import RESYNC_JOB_ID, build_scheduler, sync_schedule
from tests.test_salon_pack import SALON_ANSWERS

BAKERY_CADENCE_JOBS = 6  # rules in the bakery pack playbook


@pytest.fixture
def scheduler(container, pilot_context):
    scheduler = build_scheduler(container, BackgroundScheduler(timezone="Asia/Kolkata"))
    scheduler.start(paused=True)
    yield scheduler
    scheduler.shutdown(wait=False)


def client_job_ids(scheduler):
    return {job.id for job in scheduler.get_jobs() if job.id != RESYNC_JOB_ID}


class TestScheduleSync:
    def test_startup_schedules_every_client_plus_resync(self, container, session, scheduler):
        jobs = client_job_ids(scheduler)
        assert len(jobs) == BAKERY_CADENCE_JOBS
        assert all(job_id.startswith("pilot-1:") for job_id in jobs)
        assert scheduler.get_job(RESYNC_JOB_ID) is not None

    def test_client_onboarded_after_startup_gets_scheduled(self, container, session, scheduler):
        router = TaskRouter(container)
        ctx = container.onboarding_agent(session).run("salon-1", "salon", SALON_ANSWERS)
        container.ensure_client_tools(ctx)

        changed, removed = sync_schedule(scheduler, container, router)
        assert changed == BAKERY_CADENCE_JOBS  # the salon pack also ships 6 rules
        assert removed == 0
        assert "salon-1:engagement.weekly_broadcast" in client_job_ids(scheduler)

    def test_resync_is_idempotent(self, container, session, scheduler):
        router = TaskRouter(container)
        assert sync_schedule(scheduler, container, router) == (0, 0)

    def test_deleted_client_gets_unscheduled(self, container, session, scheduler):
        from localpulse.data.models import ClientRecord

        router = TaskRouter(container)
        record = session.get(ClientRecord, "pilot-1")
        session.delete(record)
        session.commit()

        changed, removed = sync_schedule(scheduler, container, router)
        assert removed == BAKERY_CADENCE_JOBS
        assert client_job_ids(scheduler) == set()

    def test_broken_pack_ref_skips_client_not_the_resync(self, container, session, scheduler):
        # a stale pack ref in the DB must not take the whole schedule down
        clients = ClientRepository(session)
        ctx = clients.get("pilot-1")
        broken = ctx.model_copy(update={"client_id": "ghost-1", "vertical_pack_ref": "florist"})
        clients.save(broken)

        router = TaskRouter(container)
        changed, removed = sync_schedule(scheduler, container, router)
        assert changed == 0 and removed == 0  # ghost skipped, pilot untouched
        jobs = client_job_ids(scheduler)
        assert len(jobs) == BAKERY_CADENCE_JOBS
        assert all(job_id.startswith("pilot-1:") for job_id in jobs)


class TestDispatchIsolation:
    def test_dispatch_never_raises_and_isolates_clients(self, container, session, pilot_context):
        ctx = container.onboarding_agent(session).run("salon-1", "salon", SALON_ANSWERS)
        container.ensure_client_tools(ctx)
        router = TaskRouter(container)

        original_execute = router._execute

        def explode(services, client_id, task):
            if client_id == "pilot-1":
                raise RuntimeError("boom")
            return original_execute(services, client_id, task)

        router._execute = explode
        assert router.dispatch("pilot-1", "insights.collect") is False  # contained, not raised
        assert router.dispatch("salon-1", "insights.collect") is True  # unaffected

    def test_missing_client_is_logged_not_raised(self, container, session, pilot_context):
        router = TaskRouter(container)
        assert router.dispatch("no-such-client", "insights.collect") is False

    def test_circuit_opens_after_repeated_failures_and_recovers(
        self, container, session, pilot_context
    ):
        router = TaskRouter(container)
        calls = []

        def explode(services, client_id, task):
            calls.append(task)
            raise RuntimeError("boom")

        router._execute = explode
        for _ in range(FAILURE_THRESHOLD):
            assert router.dispatch("pilot-1", "insights.collect") is False
        assert len(calls) == FAILURE_THRESHOLD

        # circuit open: the task is skipped without touching the agent
        assert router.dispatch("pilot-1", "insights.collect") is False
        assert len(calls) == FAILURE_THRESHOLD

        # ...but only for that client/task pair
        assert router.dispatch("pilot-1", "approvals.sweep_expired") is False
        assert len(calls) == FAILURE_THRESHOLD + 1

        # after the cooldown the task gets another chance
        router._now = lambda: datetime.now(UTC) + CIRCUIT_COOLDOWN + timedelta(seconds=1)
        assert router.dispatch("pilot-1", "insights.collect") is False
        assert len(calls) == FAILURE_THRESHOLD + 2

    def test_success_resets_the_failure_streak(self, container, session, pilot_context):
        router = TaskRouter(container)
        original_execute = router._execute
        fail = True

        def flaky(services, client_id, task):
            if fail:
                raise RuntimeError("boom")
            return original_execute(services, client_id, task)

        router._execute = flaky
        for _ in range(FAILURE_THRESHOLD - 1):
            router.dispatch("pilot-1", "insights.collect")
        fail = False
        assert router.dispatch("pilot-1", "insights.collect") is True
        fail = True
        # streak restarted — one new failure is nowhere near the threshold
        router.dispatch("pilot-1", "insights.collect")
        assert ("pilot-1", "insights.collect") not in router._circuit_open_until
