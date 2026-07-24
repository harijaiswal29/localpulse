"""Transient tool failures and partial broadcast delivery (spec §12.1).

Two things are proven here. First, that a temporary failure — a 429, a 503, a
dropped connection — is retried with backoff, while a permanent one is not.
Second, and this is the one that costs real money: a broadcast that dies halfway
through its recipient list resumes on the next attempt instead of starting over.
Before the delivery ledger existed, a transport error on recipient 3 of 5 meant
customers 1 and 2 received the offer twice and the shop paid for it twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import pytest

from localpulse.context.models import ApprovalState
from localpulse.context.repositories import CostLedgerRepository
from localpulse.orchestrator.cost_guard import (
    BudgetExceededError,
    CostGuard,
    MessagePurpose,
)
from localpulse.orchestrator.messaging import send_whatsapp
from localpulse.orchestrator.publisher import PartialDeliveryError, publish_draft, publish_ready
from localpulse.tools.retry import (
    DEFAULT_POLICY,
    PermanentToolError,
    RetryPolicy,
    ToolError,
    TransientToolError,
    call_with_retry,
    raise_for_response,
)
from localpulse.tools.whatsapp import (
    CloudApiWhatsAppTool,
    MockWhatsAppTool,
    OutboundMessage,
    OutboundTemplate,
)
from tests.conftest import opt_in_customer

AUDIENCE = ["+919900112233", "+919900445566", "+919900778899", "+919900001122"]
MARKETING_RATE = 0.86
UTILITY_RATE = 0.32


# --------------------------------------------------------------------------- #
# backoff policy
# --------------------------------------------------------------------------- #


class TestRetryPolicy:
    def test_delay_grows_exponentially_within_the_jitter_band(self):
        policy = RetryPolicy(base_delay=1.0, jitter=0.25, max_delay=60.0)
        for attempt, expected in ((1, 1.0), (2, 2.0), (3, 4.0)):
            delay = policy.delay_for(attempt)
            assert expected * 0.75 <= delay <= expected * 1.25

    def test_delay_is_capped(self):
        policy = RetryPolicy(base_delay=1.0, max_delay=3.0, jitter=0.0)
        assert policy.delay_for(10) == 3.0

    def test_provider_retry_after_wins_when_it_is_longer(self):
        policy = RetryPolicy(base_delay=0.5, max_delay=60.0, jitter=0.0)
        assert policy.delay_for(1, retry_after=7.0) == 7.0
        assert policy.delay_for(1, retry_after=0.1) == 0.5  # our own backoff is longer


FAST_POLICY = RetryPolicy(attempts=3, base_delay=0.01)


class TestCallWithRetry:
    def run(self, fn, policy=FAST_POLICY):
        slept: list[float] = []
        result = call_with_retry("test.op", fn, policy=policy, sleep=slept.append)
        return result, slept

    def test_a_transient_failure_is_retried_and_the_caller_never_sees_it(self):
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise TransientToolError("503 from the provider")
            return "delivered"

        result, slept = self.run(flaky)
        assert result == "delivered"
        assert attempts["n"] == 3
        assert len(slept) == 2  # one wait between each pair of attempts

    def test_a_permanent_failure_is_never_retried(self):
        attempts = {"n": 0}

        def broken():
            attempts["n"] += 1
            raise PermanentToolError("400 invalid recipient")

        with pytest.raises(PermanentToolError):
            self.run(broken)
        assert attempts["n"] == 1  # retrying would just repeat the mistake

    def test_exhausted_retries_raise_the_last_transient_error(self):
        attempts = {"n": 0}

        def always_down():
            attempts["n"] += 1
            raise TransientToolError(f"attempt {attempts['n']} failed")

        with pytest.raises(TransientToolError, match="attempt 3 failed"):
            self.run(always_down)
        assert attempts["n"] == 3

    def test_unexpected_errors_are_not_swallowed(self):
        def bug():
            raise ValueError("a real bug, not a flaky network")

        with pytest.raises(ValueError):
            self.run(bug)


# --------------------------------------------------------------------------- #
# what the WhatsApp Cloud API actually returns
# --------------------------------------------------------------------------- #


class TestCloudApiFailureClassification:
    def tool(self, attempts: int = 3) -> CloudApiWhatsAppTool:
        return CloudApiWhatsAppTool(
            client_id="pilot-1",
            api_key="k",
            phone_number_id="555",
            retry=RetryPolicy(attempts=attempts, base_delay=0.0, jitter=0.0),
        )

    def responses(self, monkeypatch, *queued: httpx.Response | Exception) -> list[int]:
        calls: list[int] = []
        pending = list(queued)

        def post(url, headers=None, json=None, timeout=None):
            calls.append(1)
            item = pending.pop(0) if pending else queued[-1]
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr("localpulse.tools.whatsapp.httpx.post", post)
        monkeypatch.setattr("localpulse.tools.retry.time.sleep", lambda _: None)
        return calls

    def test_server_error_is_retried_then_surfaces_as_transient(self, monkeypatch):
        calls = self.responses(monkeypatch, httpx.Response(500, text="internal error"))
        with pytest.raises(TransientToolError):
            self.tool().send(to=AUDIENCE[0], body="hi", category="utility")
        assert len(calls) == 3

    def test_a_rate_limit_that_clears_is_invisible_to_the_caller(self, monkeypatch):
        calls = self.responses(
            monkeypatch,
            httpx.Response(429, text="rate limited", headers={"Retry-After": "1"}),
            httpx.Response(200, json={"messages": [{"id": "wamid.OK"}]}),
        )
        assert self.tool().send(to=AUDIENCE[0], body="hi", category="utility") == "wamid.OK"
        assert len(calls) == 2

    def test_a_rejected_message_fails_immediately(self, monkeypatch):
        calls = self.responses(monkeypatch, httpx.Response(400, text="invalid recipient"))
        with pytest.raises(PermanentToolError):
            self.tool().send(to="+910000000000", body="hi", category="utility")
        assert len(calls) == 1  # no point re-sending a message Meta will not accept

    def test_an_expired_token_is_permanent(self, monkeypatch):
        self.responses(monkeypatch, httpx.Response(401, text="token expired"))
        with pytest.raises(PermanentToolError):
            self.tool().send(to=AUDIENCE[0], body="hi", category="utility")

    def test_a_dropped_connection_is_transient(self, monkeypatch):
        calls = self.responses(monkeypatch, httpx.ConnectError("connection refused"))
        with pytest.raises(TransientToolError):
            self.tool().send(to=AUDIENCE[0], body="hi", category="utility")
        assert len(calls) == 3

    def test_retry_after_is_honoured_over_our_own_backoff(self, monkeypatch):
        response = httpx.Response(429, text="slow down", headers={"Retry-After": "12"})
        with pytest.raises(TransientToolError) as caught:
            raise_for_response(response, "whatsapp.send")
        assert caught.value.retry_after == 12.0

    def test_a_date_formatted_retry_after_falls_back_to_our_backoff(self):
        response = httpx.Response(
            503, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, text=""
        )
        with pytest.raises(TransientToolError) as caught:
            raise_for_response(response, "whatsapp.send")
        assert caught.value.retry_after is None


# --------------------------------------------------------------------------- #
# broadcasts that fail halfway
# --------------------------------------------------------------------------- #


@dataclass
class FlakyWhatsAppTool:
    """Fails on chosen recipients the way a real BSP does, and records the rest."""

    client_id: str
    fail_on: dict[str, ToolError] = field(default_factory=dict)
    sent: list[OutboundMessage] = field(default_factory=list)

    def send(
        self, to: str, body: str, category: str, template: OutboundTemplate | None = None
    ) -> str:
        error = self.fail_on.get(to)
        if error is not None:
            raise error
        self.sent.append(
            OutboundMessage(
                to=to, body=body, category=category, template=template.name if template else ""
            )
        )
        return f"wa-flaky:{len(self.sent)}"

    def heal(self) -> None:
        self.fail_on.clear()


class TestBroadcastPartialDelivery:
    def setup_broadcast(self, container, session, ctx, audience=AUDIENCE):
        """An approved broadcast to `audience`, with a flaky transport installed.

        The audience is seeded first so the opt-in acknowledgements go out over the
        healthy mock — only the broadcast itself meets the flaky transport.
        """
        services = container.services(session, "pilot-1")
        for number in audience:
            opt_in_customer(services, ctx, number)
        draft = services.engagement_agent.draft_weekly_broadcast(ctx)
        approved, log_id = services.state_machine.approve(draft.id, actor="owner")
        flaky = FlakyWhatsAppTool(client_id="pilot-1")
        container.registry.register("pilot-1", "whatsapp", flaky)
        return services, approved, log_id, flaky

    def publish(self, container, services, draft, log_id):
        return publish_draft(
            draft_id=draft.id,
            approval_log_id=log_id,
            queue=services.queue,
            publish_log=services.publish_log,
            state_machine=services.state_machine,
            registry=container.registry,
            cost_guard=services.cost_guard,
            deliveries=services.deliveries,
        )

    def test_a_batch_that_dies_halfway_resumes_instead_of_starting_over(
        self, container, session, pilot_context
    ):
        services, draft, log_id, flaky = self.setup_broadcast(container, session, pilot_context)
        flaky.fail_on[AUDIENCE[2]] = TransientToolError("503 from the provider")

        with pytest.raises(PartialDeliveryError) as caught:
            self.publish(container, services, draft, log_id)
        assert caught.value.delivered == 2
        assert caught.value.remaining == 2
        assert [m.to for m in flaky.sent] == AUDIENCE[:2]
        # nothing published, so the draft is still owed to the rest
        assert services.publish_log.for_draft(draft.id) is None
        assert services.queue.get(draft.id).state == ApprovalState.APPROVED
        assert services.cost_guard.spend_this_month() == pytest.approx(2 * MARKETING_RATE)

        flaky.heal()
        action = self.publish(container, services, draft, log_id)

        assert action.external_ref == "wa-broadcast:4/4"
        assert services.queue.get(draft.id).state == ApprovalState.PUBLISHED
        delivered = [m.to for m in flaky.sent]
        assert delivered == AUDIENCE  # every number exactly once, in order
        assert len(delivered) == len(set(delivered))
        assert services.cost_guard.spend_this_month() == pytest.approx(4 * MARKETING_RATE)

    def test_the_worker_finishes_the_batch_on_the_next_tick(
        self, container, session, pilot_context
    ):
        services, draft, log_id, flaky = self.setup_broadcast(container, session, pilot_context)
        flaky.fail_on[AUDIENCE[1]] = TransientToolError("connection reset")

        assert publish_ready(services, container.registry) == []  # stalled, not lost
        assert [m.to for m in flaky.sent] == AUDIENCE[:1]

        flaky.heal()
        (action,) = publish_ready(services, container.registry)
        assert action.external_ref == "wa-broadcast:4/4"
        assert sorted(m.to for m in flaky.sent) == sorted(AUDIENCE)

    def test_a_permanently_rejected_number_is_dropped_and_the_rest_go_out(
        self, container, session, pilot_context
    ):
        services, draft, log_id, flaky = self.setup_broadcast(container, session, pilot_context)
        flaky.fail_on[AUDIENCE[1]] = PermanentToolError("400 not a WhatsApp user")

        action = self.publish(container, services, draft, log_id)

        assert action.external_ref == "wa-broadcast:3/4"  # partial beats nothing
        assert AUDIENCE[1] not in {m.to for m in flaky.sent}
        assert services.cost_guard.spend_this_month() == pytest.approx(3 * MARKETING_RATE)
        settled = {d.customer_number: d.status for d in services.deliveries.for_draft(draft.id)}
        assert settled[AUDIENCE[1]] == "failed"

    def test_a_resumed_batch_only_needs_budget_for_what_is_left(
        self, container, session, pilot_context
    ):
        services, draft, log_id, flaky = self.setup_broadcast(container, session, pilot_context)
        # exactly enough for the four recipients — and not a rupee more
        services.cost_guard = CostGuard(
            CostLedgerRepository(session, "pilot-1"), 4 * MARKETING_RATE
        )
        flaky.fail_on[AUDIENCE[2]] = TransientToolError("503 from the provider")

        with pytest.raises(PartialDeliveryError):
            self.publish(container, services, draft, log_id)
        flaky.heal()

        # a full-batch precheck would ask for 4 more and blow the budget
        action = self.publish(container, services, draft, log_id)
        assert action.external_ref == "wa-broadcast:4/4"
        assert services.cost_guard.spend_this_month() == pytest.approx(4 * MARKETING_RATE)

    def test_a_budget_block_mid_batch_leaves_the_draft_retryable(
        self, container, session, pilot_context
    ):
        services, draft, log_id, flaky = self.setup_broadcast(container, session, pilot_context)
        services.cost_guard = CostGuard(CostLedgerRepository(session, "pilot-1"), 0.0)

        with pytest.raises(BudgetExceededError):
            self.publish(container, services, draft, log_id)
        assert flaky.sent == []  # all-or-nothing precheck, before the first send
        assert services.queue.get(draft.id).state == ApprovalState.APPROVED

    def test_recipients_are_recorded_with_the_provider_reference(
        self, container, session, pilot_context
    ):
        services, draft, log_id, flaky = self.setup_broadcast(container, session, pilot_context)
        self.publish(container, services, draft, log_id)
        records = services.deliveries.for_draft(draft.id)
        assert [r.customer_number for r in records] == AUDIENCE
        assert all(r.status == "sent" and r.external_ref for r in records)

    def test_republishing_a_finished_broadcast_still_sends_nothing(
        self, container, session, pilot_context
    ):
        services, draft, log_id, flaky = self.setup_broadcast(container, session, pilot_context)
        first = self.publish(container, services, draft, log_id)
        second = self.publish(container, services, draft, log_id)
        assert second.external_ref == first.external_ref
        assert len(flaky.sent) == len(AUDIENCE)


class TestFailedSendsAreNotCharged:
    def test_a_send_the_transport_rejects_costs_nothing(self, container, session, pilot_context):
        services = container.services(session, "pilot-1")
        flaky = FlakyWhatsAppTool(
            client_id="pilot-1",
            fail_on={AUDIENCE[0]: TransientToolError("503 from the provider")},
        )
        with pytest.raises(TransientToolError):
            send_whatsapp(
                guard=services.cost_guard,
                tool=flaky,
                to=AUDIENCE[0],
                body="hello",
                purpose=MessagePurpose.REPLY,
                within_service_window=True,
            )
        assert services.cost_guard.spend_this_month() == 0.0

    def test_a_delivered_send_is_charged_exactly_once(self, container, session, pilot_context):
        services = container.services(session, "pilot-1")
        tool = MockWhatsAppTool(client_id="pilot-1")
        send_whatsapp(
            guard=services.cost_guard,
            tool=tool,
            to=AUDIENCE[0],
            body="your order is ready",
            purpose=MessagePurpose.NOTIFICATION,
            within_service_window=False,
            template=OutboundTemplate(
                name="bakery_review_nudge_v1", language="en", params=[], body="your order is ready"
            ),
        )
        assert services.cost_guard.spend_this_month() == pytest.approx(UTILITY_RATE)
        assert len(tool.sent) == 1


def test_the_default_policy_is_conservative():
    """Three attempts over a few seconds — enough to ride out a blip, not enough to
    stall a cadence tick or hammer a provider that is already struggling."""
    assert DEFAULT_POLICY.attempts == 3
    assert DEFAULT_POLICY.delay_for(DEFAULT_POLICY.attempts) <= 5.0
