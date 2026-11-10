import random
import threading
import uuid

import httpx
from fastapi.testclient import TestClient

from app.delivery import DeliveryService
from app.main import create_app
from app.worker import run_forever, run_once
from tests.conftest import make_settings
from tests.helpers import Receiver, create_endpoint, create_tenant, publish


def test_run_once_handles_a_batch_and_reports_how_many(client, make_service):
    tenant = create_tenant(client)
    create_endpoint(client, tenant)
    for _ in range(5):
        publish(client, tenant)
    receiver = Receiver(200)
    service = make_service(receiver)

    assert run_once(service, batch_size=3) == 3
    assert run_once(service, batch_size=3) == 2
    assert run_once(service, batch_size=3) == 0
    assert len(receiver.requests) == 5


def test_one_crashing_delivery_does_not_stop_the_batch(client, make_service, monkeypatch):
    tenant = create_tenant(client)
    create_endpoint(client, tenant)
    for _ in range(3):
        publish(client, tenant)
    service = make_service(Receiver(200))
    real_process = service.process
    seen = []

    def flaky(delivery_id: uuid.UUID):
        seen.append(delivery_id)
        if len(seen) == 1:
            raise RuntimeError("something unexpected")
        return real_process(delivery_id)

    monkeypatch.setattr(service, "process", flaky)

    assert run_once(service, batch_size=10) == 3  # no exception escaped
    assert len(seen) == 3


def test_the_loop_delivers_with_several_threads_until_told_to_stop(shared_database, clock):
    url, factory = shared_database
    settings = make_settings(database_url=url, worker_poll_seconds=0.01, worker_concurrency=3, worker_batch_size=4)
    app_client = TestClient(create_app(settings, factory, clock))
    tenant = create_tenant(app_client)
    create_endpoint(app_client, tenant, rate_limit_per_minute=0)
    for _ in range(12):
        publish(app_client, tenant)
    receiver = Receiver(200)
    service = DeliveryService(
        factory, settings, httpx.Client(transport=httpx.MockTransport(receiver)), clock=clock, rng=random.Random(1)
    )
    stop = threading.Event()

    thread = threading.Thread(target=run_forever, args=(service, settings, stop))
    thread.start()
    waiter = threading.Event()
    for _ in range(500):  # wait up to 5 seconds for all twelve deliveries
        if len(receiver.requests) >= 12:
            break
        waiter.wait(0.01)
    stop.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert len(receiver.requests) == 12
    assert len({r.headers["webhook-id"] for r in receiver.requests}) == 12  # every event exactly once
    assert app_client.get("/v1/deliveries?status=succeeded", headers=tenant.headers).json()["total"] == 12
