from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_settings
from tests.helpers import ADMIN_HEADERS, Receiver, create_endpoint, create_tenant, publish


def test_metrics_need_the_admin_token(client, session_factory, clock):
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"X-Admin-Token": "wrong"}).status_code == 401
    off = TestClient(create_app(make_settings(admin_token=None), session_factory, clock))
    assert off.get("/metrics").status_code == 503


def test_an_empty_system_reports_zeros(client):
    body = client.get("/metrics", headers=ADMIN_HEADERS).text

    assert 'webhook_deliveries{status="pending"} 0' in body
    assert 'webhook_deliveries{status="dead"} 0' in body
    assert "webhook_queue_lag_seconds 0.0" in body
    assert "webhook_events 0" in body


def test_metrics_reflect_what_happened(client, make_service, clock):
    tenant = create_tenant(client)
    create_endpoint(client, tenant)
    for _ in range(3):
        publish(client, tenant)
    service = make_service(Receiver(200, 500, 200))
    for delivery_id in service.claim_batch(10):
        service.process(delivery_id)
    publish(client, tenant)  # waiting and due: the queue lag starts counting
    clock.advance(30)

    response = client.get("/metrics", headers=ADMIN_HEADERS)
    body = response.text

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert 'webhook_deliveries{status="succeeded"} 2' in body
    assert 'webhook_deliveries{status="pending"} 2' in body  # one failed and waiting to retry, one new
    assert 'webhook_attempts_last_hour{outcome="success"} 2' in body
    assert 'webhook_attempts_last_hour{outcome="http_error"} 1' in body
    assert 'webhook_endpoints{circuit="closed"} 1' in body
    assert "webhook_events 4" in body
    assert "# TYPE webhook_deliveries gauge" in body
    lag = float(next(line for line in body.splitlines() if line.startswith("webhook_queue_lag_seconds")).split()[-1])
    assert lag == 30.0
