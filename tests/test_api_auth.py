from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_settings
from tests.helpers import ADMIN_HEADERS, create_tenant


def test_admin_endpoints_are_switched_off_without_an_admin_token(session_factory, clock):
    app = create_app(make_settings(admin_token=None), session_factory, clock)

    response = TestClient(app).post("/admin/tenants", json={"name": "Acme"})

    assert response.status_code == 503


def test_admin_endpoints_need_the_right_token(client):
    assert client.post("/admin/tenants", json={"name": "Acme"}).status_code == 401
    wrong = client.post("/admin/tenants", json={"name": "Acme"}, headers={"X-Admin-Token": "nope"})
    assert wrong.status_code == 401


def test_creating_a_tenant_returns_a_working_api_key_once(client):
    response = client.post("/admin/tenants", json={"name": "  Acme  "}, headers=ADMIN_HEADERS)

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Acme"
    assert body["api_key"].startswith("whp_")
    assert client.get("/v1/endpoints", headers={"Authorization": f"Bearer {body['api_key']}"}).status_code == 200


def test_api_keys_are_stored_hashed_never_in_clear(client, session_factory):
    tenant = create_tenant(client)

    from sqlalchemy import select

    from app.models import ApiKey

    with session_factory() as session:
        stored = session.scalars(select(ApiKey)).one()
    assert stored.key_hash != tenant.api_key
    assert tenant.api_key not in (stored.key_hash, stored.key_prefix)
    assert tenant.api_key.startswith(stored.key_prefix)


def test_requests_without_a_valid_key_are_refused(client):
    assert client.get("/v1/endpoints").status_code == 401
    assert (
        client.get("/v1/endpoints", headers={"Authorization": "Bearer whp_wrongwrongwrongwrongwrong"}).status_code
        == 401
    )
    assert client.get("/v1/endpoints", headers={"Authorization": "Basic abc"}).status_code == 401
    assert client.get("/v1/endpoints", headers={"Authorization": "Bearer "}).status_code == 401


def test_a_revoked_key_stops_working(client):
    tenant = create_tenant(client)
    second = client.post(f"/admin/tenants/{tenant.id}/api-keys", json={"name": "ci"}, headers=ADMIN_HEADERS).json()
    second_headers = {"Authorization": f"Bearer {second['api_key']}"}
    assert client.get("/v1/endpoints", headers=second_headers).status_code == 200

    assert client.delete(f"/admin/api-keys/{second['id']}", headers=ADMIN_HEADERS).status_code == 204

    assert client.get("/v1/endpoints", headers=second_headers).status_code == 401
    assert client.get("/v1/endpoints", headers=tenant.headers).status_code == 200  # the first key is unaffected


def test_unknown_tenant_and_key_give_404(client):
    unknown = "00000000-0000-0000-0000-000000000000"
    assert client.post(f"/admin/tenants/{unknown}/api-keys", json={}, headers=ADMIN_HEADERS).status_code == 404
    assert client.delete(f"/admin/api-keys/{unknown}", headers=ADMIN_HEADERS).status_code == 404


def test_health_reports_ok_without_any_key(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
