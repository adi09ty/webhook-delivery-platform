"""Small helpers shared by the tests."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
from fastapi.testclient import TestClient

ADMIN_TOKEN = "test-admin-token"
ADMIN_HEADERS = {"X-Admin-Token": ADMIN_TOKEN}
DEFAULT_URL = "https://hooks.example.test/in"


class FakeClock:
    """A clock the test moves by hand, so retries, cooldowns and leases can be tested without waiting."""

    def __init__(self, start: datetime | None = None):
        self.now = start or datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class TenantInfo:
    id: str
    api_key: str
    headers: dict = field(init=False)

    def __post_init__(self):
        self.headers = {"Authorization": f"Bearer {self.api_key}"}


def create_tenant(client: TestClient, name: str = "Acme") -> TenantInfo:
    response = client.post("/admin/tenants", json={"name": name}, headers=ADMIN_HEADERS)
    assert response.status_code == 201, response.text
    body = response.json()
    return TenantInfo(id=body["id"], api_key=body["api_key"])


def create_endpoint(client: TestClient, tenant: TenantInfo, url: str = DEFAULT_URL, **extra) -> dict:
    response = client.post("/v1/endpoints", json={"url": url, **extra}, headers=tenant.headers)
    assert response.status_code == 201, response.text
    return response.json()


def publish(
    client: TestClient,
    tenant: TenantInfo,
    event_type: str = "payment.completed",
    payload: dict | None = None,
    key: str | None = None,
) -> httpx.Response:
    headers = dict(tenant.headers)
    if key:
        headers["Idempotency-Key"] = key
    return client.post(
        "/v1/events",
        json={"event_type": event_type, "payload": payload if payload is not None else {"order_id": 123}},
        headers=headers,
    )


class Receiver:
    """A fake customer server for the delivery tests.

    Give it the answers to return, in order (the last one repeats). Each answer is a status code, a ready-made
    httpx.Response, or an exception to raise (to simulate a network failure). It records every request it gets.
    """

    def __init__(self, *answers):
        self.answers = list(answers) or [200]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.answers) - 1)
        answer = self.answers[index]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(answer, text="ok" if answer < 400 else "error")
