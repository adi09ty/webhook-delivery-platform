from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.main import create_app
from app.models import Delivery, Event
from tests.conftest import make_settings
from tests.helpers import create_endpoint, create_tenant, publish


def test_publishing_returns_202_and_one_delivery_per_endpoint(client):
    tenant = create_tenant(client)
    create_endpoint(client, tenant, url="https://a.example.test/in")
    create_endpoint(client, tenant, url="https://b.example.test/in")

    response = publish(client, tenant, payload={"order_id": 7})

    body = response.json()
    assert response.status_code == 202
    assert body["event_type"] == "payment.completed"
    assert body["payload"] == {"order_id": 7}
    assert body["deliveries_created"] == 2
    deliveries = client.get(f"/v1/events/{body['id']}/deliveries", headers=tenant.headers).json()
    assert len(deliveries) == 2
    assert {d["status"] for d in deliveries} == {"pending"}


def test_endpoints_only_get_the_event_types_they_asked_for(client):
    tenant = create_tenant(client)
    create_endpoint(client, tenant, url="https://all.example.test/in")
    create_endpoint(client, tenant, url="https://pay.example.test/in", event_types=["payment.completed"])
    create_endpoint(client, tenant, url="https://refund.example.test/in", event_types=["refund.created"])

    assert publish(client, tenant, "payment.completed").json()["deliveries_created"] == 2
    assert publish(client, tenant, "refund.created").json()["deliveries_created"] == 2
    assert publish(client, tenant, "customer.updated").json()["deliveries_created"] == 1


def test_disabled_endpoints_and_other_customers_endpoints_get_nothing(client):
    alice = create_tenant(client, "Alice")
    bob = create_tenant(client, "Bob")
    enabled = create_endpoint(client, alice, url="https://on.example.test/in")
    disabled = create_endpoint(client, alice, url="https://off.example.test/in")
    create_endpoint(client, bob, url="https://bob.example.test/in")
    client.patch(f"/v1/endpoints/{disabled['id']}", json={"enabled": False}, headers=alice.headers)

    body = publish(client, alice).json()

    assert body["deliveries_created"] == 1
    deliveries = client.get(f"/v1/events/{body['id']}/deliveries", headers=alice.headers).json()
    assert [d["endpoint_id"] for d in deliveries] == [enabled["id"]]
    assert client.get("/v1/deliveries", headers=bob.headers).json()["total"] == 0


def test_an_event_with_no_endpoints_is_still_accepted(client):
    tenant = create_tenant(client)

    response = publish(client, tenant)

    assert response.status_code == 202
    assert response.json()["deliveries_created"] == 0


def test_invalid_events_are_rejected(client):
    tenant = create_tenant(client)

    def post(**body):
        return client.post("/v1/events", json=body, headers=tenant.headers).status_code

    assert post(event_type="bad type!", payload={}) == 422
    assert post(event_type="", payload={}) == 422
    assert post(event_type="ok", payload=[1, 2]) == 422
    assert post(event_type="ok", payload="text") == 422
    assert post(event_type="ok") == 422
    assert post(payload={}) == 422


def test_an_oversized_payload_is_refused(session_factory, clock):
    small = TestClient(create_app(make_settings(max_payload_bytes=200), session_factory, clock))
    tenant = create_tenant(small)

    response = publish(small, tenant, payload={"blob": "x" * 500})

    assert response.status_code == 413


def test_publishing_needs_an_api_key(client):
    response = client.post("/v1/events", json={"event_type": "a", "payload": {}})
    assert response.status_code == 401


def test_the_same_idempotency_key_returns_the_first_event_not_a_second(client, session_factory):
    tenant = create_tenant(client)
    create_endpoint(client, tenant)

    first = publish(client, tenant, key="order-123-paid")
    second = publish(client, tenant, key="order-123-paid")

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["deliveries_created"] == 1
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Event)) == 1
        assert session.scalar(select(func.count()).select_from(Delivery)) == 1


def test_reusing_a_key_for_a_different_event_is_a_conflict(client):
    tenant = create_tenant(client)
    publish(client, tenant, payload={"amount": 10}, key="k1")

    different_payload = publish(client, tenant, payload={"amount": 99}, key="k1")
    different_type = publish(client, tenant, event_type="refund.created", payload={"amount": 10}, key="k1")

    assert different_payload.status_code == 409
    assert different_type.status_code == 409


def test_payload_key_order_does_not_matter_for_idempotency(client):
    tenant = create_tenant(client)
    first = publish(client, tenant, payload={"a": 1, "b": 2}, key="k")
    second = publish(client, tenant, payload={"b": 2, "a": 1}, key="k")
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


def test_idempotency_keys_are_private_to_each_customer(client):
    alice = create_tenant(client, "Alice")
    bob = create_tenant(client, "Bob")

    a = publish(client, alice, key="same-key")
    b = publish(client, bob, key="same-key")

    assert a.status_code == b.status_code == 202
    assert a.json()["id"] != b.json()["id"]


def test_events_without_a_key_are_never_treated_as_duplicates(client):
    tenant = create_tenant(client)
    assert publish(client, tenant).json()["id"] != publish(client, tenant).json()["id"]


def test_listing_filtering_and_paging_events(client):
    tenant = create_tenant(client)
    for n in range(5):
        publish(client, tenant, "payment.completed", {"n": n})
    publish(client, tenant, "refund.created", {"n": 99})

    everything = client.get("/v1/events", headers=tenant.headers).json()
    page = client.get("/v1/events?limit=2&offset=2", headers=tenant.headers).json()
    only_refunds = client.get("/v1/events?event_type=refund.created", headers=tenant.headers).json()

    assert everything["total"] == 6
    assert len(page["items"]) == 2 and page["limit"] == 2 and page["offset"] == 2
    assert only_refunds["total"] == 1
    assert client.get("/v1/events?limit=1000", headers=tenant.headers).status_code == 422
    assert client.get("/v1/events?offset=-1", headers=tenant.headers).status_code == 422


def test_one_customer_cannot_read_anothers_events(client):
    alice = create_tenant(client, "Alice")
    bob = create_tenant(client, "Bob")
    event_id = publish(client, alice).json()["id"]

    assert client.get(f"/v1/events/{event_id}", headers=bob.headers).status_code == 404
    assert client.get(f"/v1/events/{event_id}/deliveries", headers=bob.headers).status_code == 404
    assert client.get("/v1/events", headers=bob.headers).json()["total"] == 0
    assert client.get(f"/v1/events/{event_id}", headers=alice.headers).status_code == 200
