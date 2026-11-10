"""An end-to-end walkthrough against a RUNNING stack (API + worker + demo receiver).

    python -m demo.walkthrough

Settings come from environment variables:
    API_URL                http://localhost:8000            where the platform's API is
    ADMIN_TOKEN            (required)                       the platform's WHP_ADMIN_TOKEN
    RECEIVER_CONTROL_URL   http://localhost:9000            how THIS script reaches the demo receiver
    RECEIVER_HOOK_URL      http://receiver:9000/hook        how the PLATFORM reaches the demo receiver
    WAIT_SECONDS           60                               how long to wait for each step

For a quick walkthrough start the platform with short retry delays, for example WHP_RETRY_DELAYS_SECONDS=[2,2,2].
"""

import os
import sys
import time
import uuid

import httpx

API_URL = os.environ.get("API_URL", "http://localhost:8000").rstrip("/")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN") or os.environ.get("WHP_ADMIN_TOKEN", "")
CONTROL_URL = os.environ.get("RECEIVER_CONTROL_URL", "http://localhost:9000").rstrip("/")
HOOK_URL = os.environ.get("RECEIVER_HOOK_URL", "http://receiver:9000/hook")
WAIT_SECONDS = float(os.environ.get("WAIT_SECONDS", "60"))

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("  PASS  " if condition else "  FAIL  ") + message)
    if not condition:
        failures.append(message)


def wait_for(description: str, predicate, timeout: float = WAIT_SECONDS):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.5)
    check(False, f"timed out waiting for: {description}")
    return None


def main() -> int:
    if not ADMIN_TOKEN:
        print("Set ADMIN_TOKEN to the platform's WHP_ADMIN_TOKEN")
        return 2
    api = httpx.Client(base_url=API_URL, timeout=15)
    receiver = httpx.Client(base_url=CONTROL_URL, timeout=15)
    admin = {"X-Admin-Token": ADMIN_TOKEN}

    print("== Setup: a customer, an endpoint, and a receiver that knows the signing secret")
    check(api.get("/health").json() == {"status": "ok"}, "platform is healthy")
    tenant = api.post("/admin/tenants", json={"name": f"Demo {uuid.uuid4().hex[:6]}"}, headers=admin).json()
    headers = {"Authorization": f"Bearer {tenant['api_key']}"}
    endpoint = api.post("/v1/endpoints", json={"url": HOOK_URL, "rate_limit_per_minute": 0}, headers=headers).json()
    receiver.put("/_demo/secret", json={"secret": endpoint["secret"]})
    receiver.put("/_demo/mode", json={"mode": "ok"})
    receiver.delete("/_demo/received")

    def publish(payload, key=None):
        extra = {"Idempotency-Key": key} if key else {}
        return api.post(
            "/v1/events", json={"event_type": "order.paid", "payload": payload}, headers={**headers, **extra}
        )

    def delivery_of(event_id):
        items = api.get(f"/v1/events/{event_id}/deliveries", headers=headers).json()
        return items[0] if items else None

    print("== 1. Happy path: publish an event, it arrives signed")
    event = publish({"order_id": 1}).json()
    done = wait_for("delivery to succeed", lambda: (d := delivery_of(event["id"])) and d["status"] == "succeeded" and d)
    got = receiver.get("/_demo/received").json()
    check(done is not None, "delivery succeeded")
    check(got["count"] == 1 and got["items"][0]["signature_valid"], "receiver got it with a valid signature")

    print("== 2. Idempotent publish: the same Idempotency-Key never creates a second event")
    first = publish({"order_id": 2}, key="demo-key-1")
    second = publish({"order_id": 2}, key="demo-key-1")
    check(first.status_code == 202 and second.status_code == 200, "second call answered 200 (replayed)")
    check(first.json()["id"] == second.json()["id"], "both calls returned the same event")
    conflict = publish({"order_id": 999}, key="demo-key-1")
    check(conflict.status_code == 409, "same key with a different body is refused (409)")

    print("== 3. Retries: a flaky receiver fails twice, then the platform's retries get it through")
    receiver.put("/_demo/mode", json={"mode": "flaky"})
    event = publish({"order_id": 3}).json()
    done = wait_for(
        "retried delivery to succeed", lambda: (d := delivery_of(event["id"])) and d["status"] == "succeeded" and d
    )
    if done:
        detail = api.get(f"/v1/deliveries/{done['id']}", headers=headers).json()
        outcomes = [a["outcome"] for a in detail["attempts"]]
        check(outcomes == ["http_error", "http_error", "success"], f"attempts were {outcomes}")

    print("== 4. Dead-letter queue and replay: a broken receiver exhausts the retries, then we replay")
    receiver.put("/_demo/mode", json={"mode": "error500"})
    event = publish({"order_id": 4}).json()
    dead = wait_for(
        "delivery to be declared dead", lambda: (d := delivery_of(event["id"])) and d["status"] == "dead" and d
    )
    check(dead is not None, "delivery ended in the dead-letter queue")
    receiver.put("/_demo/mode", json={"mode": "ok"})
    if dead:
        replay = api.post(f"/v1/deliveries/{dead['id']}/replay", headers=headers)
        check(replay.status_code == 200 and replay.json()["status"] == "pending", "replay put it back in the queue")
        healed = wait_for(
            "replayed delivery to succeed", lambda: (d := delivery_of(event["id"])) and d["status"] == "succeeded" and d
        )
        check(healed is not None, "replayed delivery succeeded once the receiver was fixed")

    print("== 5. Tenant isolation: another customer cannot see this customer's data")
    other = api.post("/admin/tenants", json={"name": "Other"}, headers=admin).json()
    other_headers = {"Authorization": f"Bearer {other['api_key']}"}
    check(api.get(f"/v1/endpoints/{endpoint['id']}", headers=other_headers).status_code == 404, "endpoint is hidden")
    check(api.get("/v1/events", headers=other_headers).json()["total"] == 0, "events are hidden")

    print("== 6. Metrics")
    metrics = api.get("/metrics", headers=admin).text
    check('webhook_deliveries{status="succeeded"}' in metrics, "metrics endpoint answers")

    print()
    print("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
