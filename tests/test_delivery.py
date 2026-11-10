"""The delivery engine against a fake customer server (no real network, no real waiting)."""

import json
import uuid
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app import delivery as outcomes
from app import signing
from app.models import CircuitState, DeliveryAttempt, Endpoint
from tests.helpers import Receiver, create_endpoint, create_tenant, publish


@pytest.fixture
def setup(client):
    """One customer with one endpoint, ready to receive events."""
    tenant = create_tenant(client)
    endpoint = create_endpoint(client, tenant)
    return tenant, endpoint


def due(service, limit=100):
    """Claim everything that is due now and process it. Returns what happened to each delivery."""
    return [service.process(delivery_id) for delivery_id in service.claim_batch(limit)]


def load(session_factory, model, row_id):
    with session_factory() as session:
        return session.get(model, uuid.UUID(str(row_id)))


def delivery_of(client, tenant, event_id):
    return client.get(f"/v1/events/{event_id}/deliveries", headers=tenant.headers).json()[0]


# ------------------------------------------------------------------ success


def test_a_delivery_is_sent_signed_and_recorded(client, setup, make_service, session_factory, clock):
    tenant, endpoint = setup
    event = publish(client, tenant, "payment.completed", {"order_id": 123}).json()
    receiver = Receiver(200)

    assert due(make_service(receiver)) == [outcomes.SUCCEEDED]

    request = receiver.requests[0]
    body = request.content
    assert request.method == "POST"
    assert str(request.url) == endpoint["url"]
    assert request.headers["content-type"] == "application/json"
    assert request.headers["x-webhook-attempt"] == "1"
    assert json.loads(body) == {
        "id": event["id"],
        "type": "payment.completed",
        "created_at": clock().isoformat(),
        "data": {"order_id": 123},
    }
    # The receiver can prove the message is genuine using the secret it was given.
    assert signing.verify_signature(
        endpoint["secret"],
        request.headers["webhook-id"],
        request.headers["webhook-timestamp"],
        request.headers["webhook-signature"],
        body,
        now=clock().timestamp(),
    )
    assert request.headers["webhook-id"] == event["id"]

    detail = client.get(
        f"/v1/deliveries/{delivery_of(client, tenant, event['id'])['id']}", headers=tenant.headers
    ).json()
    assert detail["status"] == "succeeded"
    assert detail["attempt_count"] == 1
    assert detail["completed_at"] is not None
    assert [a["outcome"] for a in detail["attempts"]] == ["success"]
    assert detail["attempts"][0]["response_status"] == 200


def test_nothing_is_sent_when_nothing_is_due(make_service):
    receiver = Receiver(200)
    assert due(make_service(receiver)) == []
    assert receiver.requests == []


def test_a_signature_made_with_the_wrong_secret_does_not_verify(client, setup, make_service, clock):
    tenant, _ = setup
    publish(client, tenant)
    receiver = Receiver(200)
    due(make_service(receiver))
    request = receiver.requests[0]

    assert not signing.verify_signature(
        "whsec_not_the_secret",
        request.headers["webhook-id"],
        request.headers["webhook-timestamp"],
        request.headers["webhook-signature"],
        request.content,
        now=clock().timestamp(),
    )


def test_a_long_response_is_cut_down_in_the_log(client, setup, make_service):
    tenant, _ = setup
    event = publish(client, tenant).json()
    due(make_service(Receiver(httpx.Response(200, text="x" * 5000))))

    delivery_id = delivery_of(client, tenant, event["id"])["id"]
    detail = client.get(f"/v1/deliveries/{delivery_id}", headers=tenant.headers).json()

    assert len(detail["attempts"][0]["response_body"]) == 1000


# -------------------------------------------------------------------- retries


def test_a_failure_is_retried_later_and_then_succeeds(client, setup, make_service, clock):
    tenant, _ = setup
    event = publish(client, tenant).json()
    service = make_service(Receiver(500, 200))

    assert due(service) == [outcomes.RETRY_SCHEDULED]
    first = delivery_of(client, tenant, event["id"])
    assert first["status"] == "pending"
    assert first["attempt_count"] == 1
    assert first["last_error"] == "HTTP 500"
    assert first["last_status_code"] == 500

    assert due(service) == []  # not yet: the first retry is 10 seconds away
    clock.advance(9)
    assert due(service) == []
    clock.advance(1)
    assert due(service) == [outcomes.SUCCEEDED]

    done = delivery_of(client, tenant, event["id"])
    assert done["status"] == "succeeded"
    assert done["attempt_count"] == 2
    assert done["last_error"] is None


def test_the_wait_grows_with_every_failure(client, setup, make_service, clock):
    tenant, _ = setup
    event = publish(client, tenant).json()
    service = make_service(Receiver(500))
    start = clock()

    waits = []
    for _ in range(3):
        due(service)
        next_at = httpx_time(delivery_of(client, tenant, event["id"])["next_attempt_at"])
        waits.append((next_at - clock()).total_seconds())
        clock.advance(waits[-1])

    assert waits == [10, 20, 40]
    assert clock() > start


def httpx_time(text):
    return datetime.fromisoformat(text)


def test_after_the_last_retry_the_delivery_goes_to_the_dead_letter_queue(client, setup, make_service, clock):
    tenant, _ = setup
    event = publish(client, tenant).json()
    receiver = Receiver(500)
    service = make_service(receiver)

    results = []
    for _ in range(4):
        results.append(due(service))
        clock.advance(100)

    assert results == [
        [outcomes.RETRY_SCHEDULED],
        [outcomes.RETRY_SCHEDULED],
        [outcomes.RETRY_SCHEDULED],
        [outcomes.DEAD],
    ]
    assert len(receiver.requests) == 4  # 1 first try + 3 retries
    dead = delivery_of(client, tenant, event["id"])
    assert dead["status"] == "dead"
    assert dead["attempt_count"] == 4
    assert dead["last_error"] == "HTTP 500"
    assert due(service) == []  # finished: never tried again
    assert client.get("/v1/deliveries?status=dead", headers=tenant.headers).json()["total"] == 1


def test_a_network_failure_is_recorded_and_retried(client, setup, make_service):
    tenant, _ = setup
    event = publish(client, tenant).json()

    assert due(make_service(Receiver(httpx.ConnectError("connection refused")))) == [outcomes.RETRY_SCHEDULED]

    delivery_id = delivery_of(client, tenant, event["id"])["id"]
    attempt = client.get(f"/v1/deliveries/{delivery_id}", headers=tenant.headers).json()["attempts"][0]
    assert attempt["outcome"] == "network_error"
    assert attempt["response_status"] is None
    assert "ConnectError" in attempt["error"]


def test_a_timeout_is_a_failure_too(client, setup, make_service):
    tenant, _ = setup
    publish(client, tenant)

    assert due(make_service(Receiver(httpx.ReadTimeout("too slow")))) == [outcomes.RETRY_SCHEDULED]


def test_a_redirect_is_not_followed_and_counts_as_a_failure(client, setup, make_service):
    tenant, _ = setup
    event = publish(client, tenant).json()
    receiver = Receiver(httpx.Response(302, headers={"location": "http://169.254.169.254/"}))

    assert due(make_service(receiver)) == [outcomes.RETRY_SCHEDULED]

    assert len(receiver.requests) == 1  # it did not chase the redirect
    assert delivery_of(client, tenant, event["id"])["last_status_code"] == 302


def test_a_retry_after_header_pushes_the_next_try_back(client, setup, make_service, clock):
    tenant, _ = setup
    event = publish(client, tenant).json()
    due(make_service(Receiver(httpx.Response(429, headers={"retry-after": "300"}))))

    next_at = httpx_time(delivery_of(client, tenant, event["id"])["next_attempt_at"])

    assert (next_at - clock()).total_seconds() == 300  # longer than our own 10 second schedule


def test_a_huge_retry_after_is_capped(client, setup, make_service, clock):
    tenant, _ = setup
    event = publish(client, tenant).json()
    service = make_service(
        Receiver(httpx.Response(503, headers={"retry-after": "999999"})), max_retry_after_seconds=500
    )
    due(service)

    next_at = httpx_time(delivery_of(client, tenant, event["id"])["next_attempt_at"])

    assert (next_at - clock()).total_seconds() == 500


def test_410_gone_ends_the_delivery_and_switches_the_endpoint_off(client, setup, make_service):
    tenant, endpoint = setup
    event = publish(client, tenant).json()

    assert due(make_service(Receiver(410))) == [outcomes.DEAD]

    assert delivery_of(client, tenant, event["id"])["status"] == "dead"
    updated = client.get(f"/v1/endpoints/{endpoint['id']}", headers=tenant.headers).json()
    assert updated["enabled"] is False
    assert updated["disabled_reason"] == "Receiver answered 410 Gone"


def test_other_client_errors_are_retried_but_do_not_count_against_the_circuit(client, setup, make_service):
    tenant, endpoint = setup
    for _ in range(5):
        publish(client, tenant)

    results = due(make_service(Receiver(400)))

    assert results == [outcomes.RETRY_SCHEDULED] * 5
    updated = client.get(f"/v1/endpoints/{endpoint['id']}", headers=tenant.headers).json()
    assert updated["circuit_state"] == "closed"  # a 400 means the server is alive and answering
    assert updated["consecutive_failures"] == 0


def test_a_disabled_endpoint_does_not_get_deliveries(client, setup, make_service):
    tenant, endpoint = setup
    event = publish(client, tenant).json()
    client.patch(f"/v1/endpoints/{endpoint['id']}", json={"enabled": False}, headers=tenant.headers)
    receiver = Receiver(200)

    assert due(make_service(receiver)) == [outcomes.DEAD]

    assert receiver.requests == []
    assert delivery_of(client, tenant, event["id"])["last_error"] == "Endpoint is disabled"


def test_one_failing_endpoint_does_not_hold_up_a_healthy_one(client, make_service):
    tenant = create_tenant(client)
    create_endpoint(client, tenant, url="https://broken.example.test/in")
    create_endpoint(client, tenant, url="https://healthy.example.test/in")
    event = publish(client, tenant).json()

    def by_host(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500 if request.url.host == "broken.example.test" else 200)

    results = sorted(due(make_service(by_host)))

    assert results == sorted([outcomes.RETRY_SCHEDULED, outcomes.SUCCEEDED])
    statuses = {d["status"] for d in client.get(f"/v1/events/{event['id']}/deliveries", headers=tenant.headers).json()}
    assert statuses == {"pending", "succeeded"}


# ------------------------------------------------------------ circuit breaker


def test_repeated_failures_open_the_circuit_and_later_deliveries_wait(
    client, setup, make_service, session_factory, clock
):
    tenant, endpoint = setup
    for _ in range(3):
        publish(client, tenant)
    receiver = Receiver(500)
    service = make_service(receiver)
    due(service)  # three failures in a row: the threshold in the test settings
    assert load(session_factory, Endpoint, endpoint["id"]).circuit_state == CircuitState.OPEN
    sent_so_far = len(receiver.requests)

    waiting = publish(client, tenant).json()
    assert due(service) == [outcomes.DEFERRED_CIRCUIT]

    assert len(receiver.requests) == sent_so_far  # nothing was sent to the broken server
    delivery = delivery_of(client, tenant, waiting["id"])
    assert delivery["attempt_count"] == 0  # waiting did not use up one of its retries
    opened_at = load(session_factory, Endpoint, endpoint["id"]).circuit_opened_at
    assert httpx_time(delivery["next_attempt_at"]) == opened_at + timedelta(seconds=100)  # when the cooldown ends


def test_after_the_cooldown_a_successful_test_request_closes_the_circuit(
    client, setup, make_service, session_factory, clock
):
    tenant, endpoint = setup
    for _ in range(3):
        publish(client, tenant)
    service = make_service(Receiver(500, 500, 500, 200))
    due(service)
    waiting = publish(client, tenant).json()
    due(service)  # deferred while the circuit is open

    clock.advance(100)  # the cooldown is over
    results = due(service)

    assert outcomes.SUCCEEDED in results
    assert load(session_factory, Endpoint, endpoint["id"]).circuit_state == CircuitState.CLOSED
    assert delivery_of(client, tenant, waiting["id"])["status"] == "succeeded"


def test_a_failed_test_request_reopens_the_circuit(client, setup, make_service, session_factory, clock):
    tenant, endpoint = setup
    for _ in range(3):
        publish(client, tenant)
    service = make_service(Receiver(500))
    due(service)
    publish(client, tenant)
    clock.advance(100)

    due(service)

    assert load(session_factory, Endpoint, endpoint["id"]).circuit_state == CircuitState.OPEN


def test_only_one_test_request_goes_out_at_a_time(client, setup, make_service, clock):
    tenant, _ = setup
    for _ in range(3):
        publish(client, tenant)
    receiver = Receiver(500)
    service = make_service(receiver)
    due(service)
    for _ in range(3):
        publish(client, tenant)
    clock.advance(100)
    before = len(receiver.requests)

    results = due(service)

    assert len(receiver.requests) - before == 1  # exactly one test request goes out
    assert results.count(outcomes.DEFERRED_CIRCUIT) == len(results) - 1  # everything else waits


# --------------------------------------------------------------- rate limiting


def test_deliveries_over_the_endpoint_rate_limit_wait_their_turn(client, make_service, clock):
    tenant = create_tenant(client)
    create_endpoint(client, tenant, rate_limit_per_minute=2)
    for _ in range(3):
        publish(client, tenant)
    receiver = Receiver(200)
    service = make_service(receiver)

    results = due(service)

    assert sorted(results) == sorted([outcomes.SUCCEEDED, outcomes.SUCCEEDED, outcomes.DEFERRED_RATE_LIMIT])
    assert len(receiver.requests) == 2
    assert due(service) == []  # still inside the same minute
    clock.advance(61)
    assert due(service) == [outcomes.SUCCEEDED]
    assert len(receiver.requests) == 3


def test_zero_means_no_rate_limit(client, make_service):
    tenant = create_tenant(client)
    create_endpoint(client, tenant, rate_limit_per_minute=0)
    for _ in range(10):
        publish(client, tenant)

    assert due(make_service(Receiver(200))) == [outcomes.SUCCEEDED] * 10


def test_a_rate_limited_delivery_does_not_lose_a_retry(client, make_service, session_factory):
    tenant = create_tenant(client)
    create_endpoint(client, tenant, rate_limit_per_minute=1)
    events = [publish(client, tenant).json() for _ in range(2)]
    due(make_service(Receiver(200)))

    waiting = [
        delivery_of(client, tenant, e["id"])
        for e in events
        if delivery_of(client, tenant, e["id"])["status"] == "pending"
    ]

    assert len(waiting) == 1
    assert waiting[0]["attempt_count"] == 0


# ----------------------------------------------------- claiming and crash safety


def test_a_claimed_delivery_is_reserved_so_a_second_worker_cannot_take_it(client, setup, make_service, clock):
    tenant, _ = setup
    publish(client, tenant)
    service = make_service(Receiver(200))

    first_claim = service.claim_batch(10)
    second_claim = service.claim_batch(10)

    assert len(first_claim) == 1
    assert second_claim == []


def test_if_a_worker_dies_the_delivery_is_picked_up_again_after_the_reservation_expires(
    client, setup, make_service, clock
):
    tenant, _ = setup
    publish(client, tenant)
    service = make_service(Receiver(200))
    claimed = service.claim_batch(10)  # a worker claims it ... and crashes before sending anything
    assert service.claim_batch(10) == []

    clock.advance(59)
    assert service.claim_batch(10) == []
    clock.advance(2)  # the 60 second reservation has run out
    recovered = service.claim_batch(10)

    assert recovered == claimed
    assert service.process(recovered[0]) == outcomes.SUCCEEDED


def test_two_workers_never_share_a_delivery(client, setup, make_service):
    tenant, _ = setup
    for _ in range(6):
        publish(client, tenant)
    worker_a = make_service(Receiver(200))
    worker_b = make_service(Receiver(200))

    a = worker_a.claim_batch(4)
    b = worker_b.claim_batch(4)

    assert len(a) == 4 and len(b) == 2
    assert not set(a) & set(b)


def test_a_finished_delivery_is_skipped_if_it_is_somehow_processed_again(client, setup, make_service):
    tenant, _ = setup
    event = publish(client, tenant).json()
    service = make_service(Receiver(200))
    delivery_id = uuid.UUID(delivery_of(client, tenant, event["id"])["id"])
    assert service.process(delivery_id) == outcomes.SUCCEEDED

    assert service.process(delivery_id) == outcomes.SKIPPED
    assert service.process(uuid.uuid4()) == outcomes.SKIPPED


# ----------------------------------------------------------------- safety check


def test_an_unsafe_address_is_blocked_at_send_time_even_if_it_got_registered(
    client, setup, make_service, session_factory
):
    tenant, endpoint = setup
    publish(client, tenant)
    with session_factory() as session, session.begin():
        session.get(Endpoint, uuid.UUID(endpoint["id"])).url = "https://169.254.169.254/latest/meta-data"
    receiver = Receiver(200)

    results = due(make_service(receiver, allow_private_targets=False))

    assert results == [outcomes.RETRY_SCHEDULED]
    assert receiver.requests == []  # never sent
    with session_factory() as session:
        attempt = session.scalars(select(DeliveryAttempt)).one()
    assert attempt.outcome == "blocked"
    assert "non-public" in attempt.error


# ----------------------------------------------------------------------- replay


def make_dead(client, tenant, make_service, clock):
    event = publish(client, tenant).json()
    service = make_service(Receiver(500))
    for _ in range(4):
        due(service)
        clock.advance(100)
    return event, service


def test_a_dead_delivery_can_be_replayed_once_the_receiver_is_fixed(client, setup, make_service, clock):
    tenant, _ = setup
    event, _ = make_dead(client, tenant, make_service, clock)
    dead = delivery_of(client, tenant, event["id"])
    assert dead["status"] == "dead"

    replay = client.post(f"/v1/deliveries/{dead['id']}/replay", headers=tenant.headers)

    assert replay.status_code == 200
    assert replay.json()["status"] == "pending"
    assert replay.json()["attempt_count"] == 0  # a fresh set of retries
    assert replay.json()["replay_count"] == 1
    assert due(make_service(Receiver(200))) == [outcomes.SUCCEEDED]
    healed = client.get(f"/v1/deliveries/{dead['id']}", headers=tenant.headers).json()
    assert healed["status"] == "succeeded"
    assert len(healed["attempts"]) == 5  # the history of the failures is kept


def test_a_delivery_that_is_still_waiting_cannot_be_replayed(client, setup):
    tenant, _ = setup
    event = publish(client, tenant).json()
    delivery_id = delivery_of(client, tenant, event["id"])["id"]

    assert client.post(f"/v1/deliveries/{delivery_id}/replay", headers=tenant.headers).status_code == 409


def test_replaying_to_a_disabled_endpoint_is_refused(client, setup, make_service, clock):
    tenant, endpoint = setup
    event, _ = make_dead(client, tenant, make_service, clock)
    delivery_id = delivery_of(client, tenant, event["id"])["id"]
    client.patch(f"/v1/endpoints/{endpoint['id']}", json={"enabled": False}, headers=tenant.headers)

    assert client.post(f"/v1/deliveries/{delivery_id}/replay", headers=tenant.headers).status_code == 409
    assert client.post(f"/v1/endpoints/{endpoint['id']}/replay-dead", headers=tenant.headers).status_code == 409


def test_all_dead_deliveries_of_an_endpoint_can_be_replayed_in_one_go(client, setup, make_service, clock):
    tenant, endpoint = setup
    for _ in range(3):
        publish(client, tenant)
    # A very high circuit threshold keeps the breaker out of this test, so the three really exhaust their retries.
    service = make_service(Receiver(500), circuit_failure_threshold=1000)
    for _ in range(4):
        due(service)
        clock.advance(100)
    assert client.get("/v1/deliveries?status=dead", headers=tenant.headers).json()["total"] == 3

    result = client.post(f"/v1/endpoints/{endpoint['id']}/replay-dead", headers=tenant.headers)

    assert result.json() == {"replayed": 3}
    assert client.get("/v1/deliveries?status=pending", headers=tenant.headers).json()["total"] == 3


def test_one_customer_cannot_replay_anothers_delivery(client, setup, make_service, clock):
    tenant, _ = setup
    other = create_tenant(client, "Mallory")
    event, _ = make_dead(client, tenant, make_service, clock)
    delivery_id = delivery_of(client, tenant, event["id"])["id"]

    assert client.post(f"/v1/deliveries/{delivery_id}/replay", headers=other.headers).status_code == 404
    assert client.get(f"/v1/deliveries/{delivery_id}", headers=other.headers).status_code == 404
    assert client.get("/v1/deliveries", headers=other.headers).json()["total"] == 0


def test_listing_deliveries_with_filters_and_paging(client, setup, make_service):
    tenant, endpoint = setup
    for _ in range(5):
        publish(client, tenant)
    due(make_service(Receiver(200)))

    everything = client.get("/v1/deliveries", headers=tenant.headers).json()
    paged = client.get("/v1/deliveries?limit=2&offset=4", headers=tenant.headers).json()
    by_endpoint = client.get(f"/v1/deliveries?endpoint_id={endpoint['id']}", headers=tenant.headers).json()

    assert everything["total"] == 5
    assert len(paged["items"]) == 1
    assert by_endpoint["total"] == 5
    assert client.get("/v1/deliveries?status=succeeded", headers=tenant.headers).json()["total"] == 5
    assert client.get("/v1/deliveries?status=bogus", headers=tenant.headers).status_code == 422
    assert client.get(f"/v1/deliveries/{uuid.uuid4()}", headers=tenant.headers).status_code == 404
