"""Real threads, real transactions: identical publish requests sent at the same instant."""

import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.main import create_app
from app.models import Delivery, Event
from tests.conftest import make_settings
from tests.helpers import create_endpoint, create_tenant

THREADS = 8


def test_identical_publishes_at_the_same_moment_create_exactly_one_event(shared_database, clock):
    url, factory = shared_database
    app = create_app(make_settings(database_url=url), factory, clock)
    setup_client = TestClient(app)
    tenant = create_tenant(setup_client)
    create_endpoint(setup_client, tenant)
    headers = {**tenant.headers, "Idempotency-Key": "race-key-1"}
    body = {"event_type": "payment.completed", "payload": {"order_id": 1}}
    start_together = threading.Barrier(THREADS)

    def publish_now(_):
        start_together.wait()
        return TestClient(app).post("/v1/events", json=body, headers=headers)

    with ThreadPoolExecutor(THREADS) as pool:
        responses = list(pool.map(publish_now, range(THREADS)))

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200] * (THREADS - 1) + [202], statuses  # one winner, everyone else gets the same event
    assert len({r.json()["id"] for r in responses}) == 1
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Event)) == 1
        assert session.scalar(select(func.count()).select_from(Delivery)) == 1
