"""Circuit breaker: stop hammering an endpoint that is clearly down.

States
    closed     normal: every delivery is tried
    open       too many failures in a row: do not try for `cooldown` seconds (deliveries simply wait)
    half_open  cooldown over: allow ONE test request. Success closes the circuit, failure opens it again.

These are plain functions on the Endpoint row, so the logic can be tested without a database. The caller is
responsible for locking the row while it decides (see delivery.py).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.models import CircuitState, Endpoint


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_at: datetime | None = None  # when to look again, if not allowed


def check(endpoint: Endpoint, now: datetime, *, cooldown_seconds: int, probe_window_seconds: int) -> Decision:
    """May we send to this endpoint right now? May change the endpoint (open -> half_open, probe bookkeeping)."""
    if endpoint.circuit_state == CircuitState.CLOSED:
        return Decision(True)

    if endpoint.circuit_state == CircuitState.OPEN:
        assert endpoint.circuit_opened_at is not None
        reopen_at = endpoint.circuit_opened_at + timedelta(seconds=cooldown_seconds)
        if now < reopen_at:
            return Decision(False, reopen_at)
        endpoint.circuit_state = CircuitState.HALF_OPEN
        endpoint.probe_started_at = now
        return Decision(True)  # this request is the test request

    # half_open: one test request at a time. If one is already in flight, wait for its result.
    if endpoint.probe_started_at is not None:
        probe_deadline = endpoint.probe_started_at + timedelta(seconds=probe_window_seconds)
        if now < probe_deadline:
            return Decision(False, probe_deadline)
    endpoint.probe_started_at = now  # no probe in flight (or it never reported back): start a new one
    return Decision(True)


def on_success(endpoint: Endpoint) -> None:
    endpoint.circuit_state = CircuitState.CLOSED
    endpoint.consecutive_failures = 0
    endpoint.circuit_opened_at = None
    endpoint.probe_started_at = None


def on_failure(endpoint: Endpoint, now: datetime, *, threshold: int) -> None:
    endpoint.consecutive_failures += 1
    if endpoint.circuit_state == CircuitState.HALF_OPEN or endpoint.consecutive_failures >= threshold:
        endpoint.circuit_state = CircuitState.OPEN
        endpoint.circuit_opened_at = now
        endpoint.probe_started_at = None
