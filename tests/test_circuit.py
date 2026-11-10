"""The circuit breaker as a plain state machine, no database involved."""

from datetime import UTC, datetime, timedelta

from app import circuit
from app.models import CircuitState, Endpoint

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
COOLDOWN = 100
PROBE_WINDOW = 30
THRESHOLD = 3


def make_endpoint() -> Endpoint:
    # Column defaults are applied when a row is saved, so set the health fields explicitly here.
    return Endpoint(
        circuit_state=CircuitState.CLOSED, consecutive_failures=0, circuit_opened_at=None, probe_started_at=None
    )


def check(endpoint, now=NOW):
    return circuit.check(endpoint, now, cooldown_seconds=COOLDOWN, probe_window_seconds=PROBE_WINDOW)


def fail(endpoint, now=NOW):
    circuit.on_failure(endpoint, now, threshold=THRESHOLD)


def trip(endpoint, now=NOW):
    for _ in range(THRESHOLD):
        fail(endpoint, now)


def test_a_healthy_endpoint_is_always_allowed():
    assert check(make_endpoint()).allowed


def test_failures_below_the_threshold_keep_it_closed():
    endpoint = make_endpoint()
    fail(endpoint)
    fail(endpoint)
    assert endpoint.circuit_state == CircuitState.CLOSED
    assert check(endpoint).allowed


def test_reaching_the_threshold_opens_it():
    endpoint = make_endpoint()
    trip(endpoint)
    assert endpoint.circuit_state == CircuitState.OPEN
    assert endpoint.circuit_opened_at == NOW


def test_an_open_circuit_refuses_until_the_cooldown_ends_and_says_when_to_come_back():
    endpoint = make_endpoint()
    trip(endpoint)

    decision = check(endpoint, NOW + timedelta(seconds=COOLDOWN - 1))

    assert not decision.allowed
    assert decision.retry_at == NOW + timedelta(seconds=COOLDOWN)
    assert endpoint.circuit_state == CircuitState.OPEN


def test_after_the_cooldown_one_test_request_is_allowed():
    endpoint = make_endpoint()
    trip(endpoint)

    decision = check(endpoint, NOW + timedelta(seconds=COOLDOWN))

    assert decision.allowed
    assert endpoint.circuit_state == CircuitState.HALF_OPEN


def test_only_one_test_request_at_a_time():
    endpoint = make_endpoint()
    trip(endpoint)
    probe_time = NOW + timedelta(seconds=COOLDOWN)
    assert check(endpoint, probe_time).allowed

    second = check(endpoint, probe_time + timedelta(seconds=5))

    assert not second.allowed
    assert second.retry_at == probe_time + timedelta(seconds=PROBE_WINDOW)


def test_a_test_request_that_never_reports_back_is_replaced_after_the_window():
    endpoint = make_endpoint()
    trip(endpoint)
    probe_time = NOW + timedelta(seconds=COOLDOWN)
    check(endpoint, probe_time)

    assert check(endpoint, probe_time + timedelta(seconds=PROBE_WINDOW)).allowed


def test_a_successful_test_request_closes_the_circuit_and_clears_the_record():
    endpoint = make_endpoint()
    trip(endpoint)
    check(endpoint, NOW + timedelta(seconds=COOLDOWN))

    circuit.on_success(endpoint)

    assert endpoint.circuit_state == CircuitState.CLOSED
    assert endpoint.consecutive_failures == 0
    assert endpoint.circuit_opened_at is None
    assert endpoint.probe_started_at is None


def test_a_failed_test_request_reopens_it_immediately():
    endpoint = make_endpoint()
    trip(endpoint)
    reopen_time = NOW + timedelta(seconds=COOLDOWN)
    check(endpoint, reopen_time)

    fail(endpoint, reopen_time)

    assert endpoint.circuit_state == CircuitState.OPEN
    assert endpoint.circuit_opened_at == reopen_time
    assert not check(endpoint, reopen_time + timedelta(seconds=1)).allowed


def test_a_success_resets_the_failure_streak():
    endpoint = make_endpoint()
    fail(endpoint)
    fail(endpoint)
    circuit.on_success(endpoint)
    fail(endpoint)
    fail(endpoint)
    assert endpoint.circuit_state == CircuitState.CLOSED
