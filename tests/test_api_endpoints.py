import uuid

from fastapi.testclient import TestClient

from app.main import create_app
from app.models import CircuitState, Endpoint
from tests.conftest import make_settings
from tests.helpers import DEFAULT_URL, create_endpoint, create_tenant, publish


def test_creating_an_endpoint_returns_its_signing_secret_once(client):
    tenant = create_tenant(client)

    created = create_endpoint(client, tenant, description="Billing hooks", event_types=["payment.completed"])

    assert created["secret"].startswith("whsec_")
    assert created["enabled"] is True
    assert created["circuit_state"] == "closed"
    assert created["rate_limit_per_minute"] == 60  # the platform default
    fetched = client.get(f"/v1/endpoints/{created['id']}", headers=tenant.headers).json()
    assert "secret" not in fetched
    listed = client.get("/v1/endpoints", headers=tenant.headers).json()
    assert [e["id"] for e in listed] == [created["id"]]
    assert "secret" not in listed[0]


def test_a_custom_rate_limit_is_kept(client):
    tenant = create_tenant(client)
    assert create_endpoint(client, tenant, rate_limit_per_minute=5)["rate_limit_per_minute"] == 5
    assert create_endpoint(client, tenant, rate_limit_per_minute=0)["rate_limit_per_minute"] == 0


def test_bad_input_is_rejected(client):
    tenant = create_tenant(client)

    def post(**body):
        return client.post("/v1/endpoints", json=body, headers=tenant.headers).status_code

    assert post(url="ftp://example.com") == 422
    assert post(url="not a url") == 422
    assert post(url=DEFAULT_URL, rate_limit_per_minute=-1) == 422
    assert post(url=DEFAULT_URL, event_types=[""]) == 422
    assert post(url=DEFAULT_URL, event_types=["x"] * 51) == 422
    assert post(url=DEFAULT_URL, description="d" * 256) == 422
    assert post() == 422


def test_production_mode_refuses_endpoints_that_point_inside_the_network(session_factory, clock):
    strict = TestClient(create_app(make_settings(allow_private_targets=False), session_factory, clock))
    tenant = create_tenant(strict)

    for url in ("https://127.0.0.1/hook", "https://169.254.169.254/", "http://93.184.216.34/hook"):
        response = strict.post("/v1/endpoints", json={"url": url}, headers=tenant.headers)
        assert response.status_code == 422, url
    ok = strict.post("/v1/endpoints", json={"url": "https://93.184.216.34/hook"}, headers=tenant.headers)
    assert ok.status_code == 201


def test_updating_an_endpoint(client):
    tenant = create_tenant(client)
    endpoint = create_endpoint(client, tenant)

    response = client.patch(
        f"/v1/endpoints/{endpoint['id']}",
        json={"url": "https://other.example.test/in", "event_types": ["a", "b"], "rate_limit_per_minute": 10},
        headers=tenant.headers,
    )

    body = response.json()
    assert response.status_code == 200
    assert body["url"] == "https://other.example.test/in"
    assert body["event_types"] == ["a", "b"]
    assert body["rate_limit_per_minute"] == 10


def test_an_update_with_a_bad_url_changes_nothing(client):
    tenant = create_tenant(client)
    endpoint = create_endpoint(client, tenant)

    response = client.patch(f"/v1/endpoints/{endpoint['id']}", json={"url": "ftp://x"}, headers=tenant.headers)

    assert response.status_code == 422
    assert client.get(f"/v1/endpoints/{endpoint['id']}", headers=tenant.headers).json()["url"] == DEFAULT_URL


def test_enabling_an_endpoint_resets_its_health_record(client, session_factory):
    tenant = create_tenant(client)
    endpoint = create_endpoint(client, tenant)
    with session_factory() as session, session.begin():
        row = session.get(Endpoint, uuid.UUID(endpoint["id"]))
        row.enabled = False
        row.disabled_reason = "Receiver answered 410 Gone"
        row.circuit_state = CircuitState.OPEN
        row.consecutive_failures = 9

    response = client.patch(f"/v1/endpoints/{endpoint['id']}", json={"enabled": True}, headers=tenant.headers).json()

    assert response["enabled"] is True
    assert response["disabled_reason"] is None
    assert response["circuit_state"] == "closed"
    assert response["consecutive_failures"] == 0


def test_rotating_the_secret_gives_a_new_one(client):
    tenant = create_tenant(client)
    endpoint = create_endpoint(client, tenant)

    rotated = client.post(f"/v1/endpoints/{endpoint['id']}/rotate-secret", headers=tenant.headers).json()

    assert rotated["secret"].startswith("whsec_")
    assert rotated["secret"] != endpoint["secret"]


def test_deleting_an_endpoint_removes_it_and_its_deliveries(client):
    tenant = create_tenant(client)
    endpoint = create_endpoint(client, tenant)
    publish(client, tenant)
    assert client.get("/v1/deliveries", headers=tenant.headers).json()["total"] == 1

    assert client.delete(f"/v1/endpoints/{endpoint['id']}", headers=tenant.headers).status_code == 204

    assert client.get(f"/v1/endpoints/{endpoint['id']}", headers=tenant.headers).status_code == 404
    assert client.get("/v1/deliveries", headers=tenant.headers).json()["total"] == 0


def test_one_customer_cannot_see_or_touch_anothers_endpoints(client):
    alice = create_tenant(client, "Alice")
    bob = create_tenant(client, "Bob")
    endpoint = create_endpoint(client, alice)
    path = f"/v1/endpoints/{endpoint['id']}"

    assert client.get(path, headers=bob.headers).status_code == 404
    assert client.patch(path, json={"enabled": False}, headers=bob.headers).status_code == 404
    assert client.delete(path, headers=bob.headers).status_code == 404
    assert client.post(f"{path}/rotate-secret", headers=bob.headers).status_code == 404
    assert client.post(f"{path}/replay-dead", headers=bob.headers).status_code == 404
    assert client.get("/v1/endpoints", headers=bob.headers).json() == []
    assert client.get(path, headers=alice.headers).status_code == 200  # untouched
