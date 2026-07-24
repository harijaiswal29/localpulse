"""Transient-failure handling for the tool layer (spec §12.1).

External tools fail often, and most of those failures are temporary: a 429 from
Meta, a 503 from Google, a socket that timed out. Retrying those with backoff
turns a class of would-be incidents into a slightly slower send.

Retrying a *permanent* failure only repeats the mistake — and on a paid channel
it can repeat the charge — so the two are separated here and only transient
errors are retried. Callers upstream read the distinction the same way: a
`TransientToolError` that survives its retries means "try again on the next
cadence tick"; a `PermanentToolError` means "this will never work".
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# Worth another attempt: rate limits, request timeouts, and server-side faults.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class ToolError(Exception):
    """Base for failures raised by the tool layer."""


class TransientToolError(ToolError):
    """Temporary — the same call may well succeed a moment later."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class PermanentToolError(ToolError):
    """Will fail identically however many times it is retried."""


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3  # total attempts, not extra ones
    base_delay: float = 0.5  # seconds before the first retry
    max_delay: float = 8.0
    jitter: float = 0.25  # ± fraction, so many clients never retry in lockstep

    def delay_for(self, attempt: int, retry_after: float | None = None) -> float:
        """How long to wait after `attempt` (1-based) has failed."""
        delay = self.base_delay * (2 ** (attempt - 1))
        if retry_after is not None:
            delay = max(delay, retry_after)  # the provider's own hint wins if longer
        delay = min(delay, self.max_delay)
        spread = delay * self.jitter
        return max(0.0, delay + random.uniform(-spread, spread))


DEFAULT_POLICY = RetryPolicy()


def call_with_retry[T](
    operation: str,
    fn: Callable[[], T],
    *,
    policy: RetryPolicy = DEFAULT_POLICY,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run `fn`, retrying transient failures with exponential backoff.

    `sleep` is injectable so tests exercise the backoff without waiting for it.
    Permanent failures propagate on the first attempt, untouched.
    """
    last_error: TransientToolError | None = None
    for attempt in range(1, policy.attempts + 1):
        try:
            return fn()
        except TransientToolError as exc:
            last_error = exc
            if attempt == policy.attempts:
                break
            pause = policy.delay_for(attempt, exc.retry_after)
            logger.warning(
                "%s failed (attempt %d/%d): %s — retrying in %.2fs",
                operation,
                attempt,
                policy.attempts,
                exc,
                pause,
            )
            sleep(pause)
    assert last_error is not None  # the loop only breaks after a transient failure
    logger.error("%s failed after %d attempts: %s", operation, policy.attempts, last_error)
    raise last_error


def raise_for_response(response: httpx.Response, operation: str) -> None:
    """Turn an error response into the right kind of `ToolError` — or return quietly."""
    if response.status_code < 400:
        return
    detail = f"{operation}: HTTP {response.status_code} — {response.text[:300]}"
    if response.status_code in RETRYABLE_STATUS:
        raise TransientToolError(detail, retry_after=_retry_after(response))
    raise PermanentToolError(detail)


def transient_from(exc: httpx.RequestError, operation: str) -> TransientToolError:
    """Connection reset, DNS failure, read timeout — the request never landed."""
    return TransientToolError(f"{operation}: {type(exc).__name__} — {exc}")


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    try:
        return float(raw) if raw else None
    except (TypeError, ValueError):
        return None  # HTTP-date form; our own backoff covers it
