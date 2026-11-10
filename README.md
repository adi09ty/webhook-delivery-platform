# Webhook Delivery Platform

Reliable webhook delivery as a service, in Python. A company publishes **events** through an API, and the platform delivers each event to every URL (**endpoint**) the company registered: signed with HMAC, retried with back-off, logged attempt by attempt, parked in a dead-letter queue when it keeps failing, and replayable once the receiving server is fixed.

It solves the problem every SaaS product has: pushing events to customers' servers when those servers are slow, down, duplicated or hostile.

## Features
- **Multi-tenant API** with hashed API keys; every query is scoped to the customer, and another customer's data looks like it does not exist (404)
- **Endpoints** per customer: event-type filters, per-endpoint rate limit, enable/disable, secret rotation
- **Signed requests** (HMAC-SHA256, "Standard Webhooks" style headers) with a timestamp so receivers can reject replays
- **Retries with exponential back-off and jitter**, honouring `Retry-After`; `410 Gone` switches an endpoint off
- **Delivery log**: every attempt is stored (status, duration, response snippet, error)
- **Dead-letter queue and replay**: single delivery or all dead deliveries of an endpoint, with a fresh set of retries
- **Idempotent publishing**: an `Idempotency-Key` makes retried publish calls safe, even when two identical requests arrive at the same instant (enforced by a unique database constraint)
- **Circuit breaker** per endpoint (closed, open, half-open with a single test request) so a dead server is not hammered
- **Per-endpoint rate limiting** across any number of workers
- **At-least-once delivery that survives crashes**: workers reserve jobs with a lease; if a worker dies the job is picked up again
- **SSRF protection**: private, loopback and cloud-metadata addresses are refused, checked at registration and again at send time; redirects are never followed
- **Metrics** in Prometheus format, read from the database so they cover the API and every worker
- Database migrations (Alembic), Docker Compose with PostgreSQL, CI, a demo receiver and an end-to-end walkthrough script

## Tech stack
Python 3.13, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL (SQLite for quick runs and tests), Pydantic, httpx, pytest, ruff, Docker, GitHub Actions.

## Architecture
```
 your app --POST /v1/events-->  API (FastAPI)  ----------------+
                                   |                            |
                                   |  one transaction:          |  GET /v1/deliveries, /metrics ...
                                   |  event + one delivery      |
                                   v  job per matching endpoint |
                              PostgreSQL  <---------------------+
                          (events, deliveries = the queue,
                           attempts, endpoints, tenants)
                                   ^
                                   |  claim due jobs (FOR UPDATE SKIP LOCKED, with a lease)
                              Worker(s)  --HMAC-signed POST-->  customer's server
                                   |
                                   +--> record attempt, then: succeeded | retry later | dead
```

### How one delivery works
1. **Publish.** The API stores the event and one `delivery` row per matching, enabled endpoint **in a single transaction**: an accepted event can never exist without its delivery jobs.
2. **Claim.** A worker selects due deliveries with `FOR UPDATE SKIP LOCKED` and reserves them for 60 seconds. Several workers can run at once and never take the same job.
3. **Prepare.** In a short transaction the endpoint row is locked and the safety checks run: endpoint enabled? rate limit? circuit open?
4. **Send.** The HTTP request is made **outside any database transaction**, with a timeout, no redirects, and a size-limited read of the answer.
5. **Record.** In a second short transaction the attempt is saved and the delivery becomes `succeeded`, is scheduled for a retry, or becomes `dead`.

## What the receiver gets
```
POST https://customer.example/hook
content-type: application/json
webhook-id: 7b1f...            <- the event id, identical on every retry, so receivers can ignore repeats
webhook-timestamp: 1767268800
webhook-signature: v1,hG3k...  <- base64 HMAC-SHA256 of "<id>.<timestamp>.<body>" with the endpoint secret
x-webhook-attempt: 2

{"created_at":"2026-01-01T12:00:00+00:00","data":{"order_id":123},"id":"7b1f...","type":"payment.completed"}
```
Verifying on the receiving side:
```python
import base64, hashlib, hmac, time


def verify(secret: str, msg_id: str, timestamp: str, signature_header: str, body: bytes) -> bool:
    if abs(time.time() - int(timestamp)) > 300:  # reject old messages (replay attacks)
        return False
    signed = f"{msg_id}.{timestamp}.".encode() + body
    expected = "v1," + base64.b64encode(hmac.new(secret.encode(), signed, hashlib.sha256).digest()).decode()
    return any(hmac.compare_digest(s, expected) for s in signature_header.split())
```

## Run it

### With Docker Compose (PostgreSQL + API + worker + demo receiver)
```bash
cp .env.example .env        # then set your own DB_PASSWORD and ADMIN_TOKEN
docker compose up --build
```
- API and interactive docs: http://localhost:8000/docs
- Demo receiver: http://localhost:9000

Run the end-to-end walkthrough (happy path, idempotency, retries, dead-letter and replay, tenant isolation, metrics):
```bash
docker compose exec -e RECEIVER_CONTROL_URL=http://receiver:9000 api python -m demo.walkthrough
```
The compose file uses short retry delays (2 seconds) so a demo takes seconds, and allows private targets because the demo receiver is on a private address. Production defaults are 30 s, 2 min, 10 min, 1 h, 6 h and private targets refused.

### Locally without Docker (SQLite)
```bash
python -m venv .venv && .venv\Scripts\activate          # use python 3.13
pip install -r requirements.txt
set WHP_ADMIN_TOKEN=dev-token
alembic upgrade head
uvicorn app.main:app --reload                            # terminal 1: the API
python -m app.worker                                     # terminal 2: the worker
```

## Quick tour with curl
```bash
# 1. As the operator: create a customer. The API key is shown once.
curl -X POST localhost:8000/admin/tenants -H "X-Admin-Token: $ADMIN_TOKEN" \
     -H "Content-Type: application/json" -d '{"name":"Acme"}'

# 2. As the customer: register an endpoint. The signing secret is shown once.
curl -X POST localhost:8000/v1/endpoints -H "Authorization: Bearer $API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"url":"https://customer.example/hook","event_types":["payment.completed"]}'

# 3. Publish an event (the Idempotency-Key makes retrying this call safe).
curl -X POST localhost:8000/v1/events -H "Authorization: Bearer $API_KEY" \
     -H "Idempotency-Key: order-123-paid" -H "Content-Type: application/json" \
     -d '{"event_type":"payment.completed","payload":{"order_id":123,"amount":499}}'

# 4. Look at what happened, and replay a dead delivery.
curl localhost:8000/v1/deliveries?status=dead -H "Authorization: Bearer $API_KEY"
curl -X POST localhost:8000/v1/deliveries/$DELIVERY_ID/replay -H "Authorization: Bearer $API_KEY"
```

## API overview
| Method and path | Purpose |
|---|---|
| `POST /admin/tenants`, `POST /admin/tenants/{id}/api-keys`, `DELETE /admin/api-keys/{id}` | Operator: customers and keys (`X-Admin-Token`) |
| `POST/GET /v1/endpoints`, `GET/PATCH/DELETE /v1/endpoints/{id}` | Manage endpoints |
| `POST /v1/endpoints/{id}/rotate-secret` | New signing secret |
| `POST /v1/endpoints/{id}/replay-dead` | Requeue every dead delivery of an endpoint |
| `POST /v1/events` | Publish an event (202; 200 when replayed by `Idempotency-Key`; 409 if the key was used for different content) |
| `GET /v1/events`, `GET /v1/events/{id}`, `GET /v1/events/{id}/deliveries` | Read events |
| `GET /v1/deliveries`, `GET /v1/deliveries/{id}` | Delivery log with attempts; filter by status, endpoint, event |
| `POST /v1/deliveries/{id}/replay` | Send a finished delivery again |
| `GET /health`, `GET /metrics` | Health; Prometheus metrics (`X-Admin-Token`) |

Status codes: `202` accepted, `200` replayed, `401` bad or missing key, `404` not found (also for other customers' data), `409` conflict (idempotency key reused with different content, or replaying to a disabled endpoint), `413` payload too large, `422` validation error (including unsafe URLs).

## Configuration
Settings are environment variables with the prefix `WHP_` (see `app/config.py` for all of them).

| Variable | Default | Meaning |
|---|---|---|
| `WHP_DATABASE_URL` | `sqlite:///./webhooks.db` | Use `postgresql+psycopg://user:pass@host/db` for real use |
| `WHP_ADMIN_TOKEN` | none | Enables `/admin` and `/metrics`; without it they are switched off |
| `WHP_ALLOW_PRIVATE_TARGETS` | `false` | Allow delivery to private addresses (demos only) |
| `WHP_RETRY_DELAYS_SECONDS` | `[30,120,600,3600,21600]` | Wait after each failure; one more failure afterwards means dead |
| `WHP_CIRCUIT_FAILURE_THRESHOLD` / `WHP_CIRCUIT_COOLDOWN_SECONDS` | `5` / `300` | Breaker opens after this many failures in a row, for this long |
| `WHP_DEFAULT_RATE_LIMIT_PER_MINUTE` | `60` | Per-endpoint default (0 means unlimited) |
| `WHP_WORKER_CONCURRENCY` / `WHP_WORKER_BATCH_SIZE` | `4` / `10` | Threads per worker, jobs claimed per round |
| `WHP_LEASE_SECONDS` | `60` | How long a claimed job is reserved before another worker may take it |

## Tests
```bash
pip install -r requirements-dev.txt
pytest --cov=app
```
122 tests, about 96% line coverage. They cover:
- **Signing:** exact recipe, tampering, wrong secret, old and future timestamps, rotation (several signatures)
- **Delivery engine** against a fake customer server: success, signed headers verified, retry schedule and growth, exhausting retries into the dead-letter queue, network errors and timeouts, `Retry-After`, `410 Gone`, redirects not followed, circuit breaker states (open, half-open probe, reopen, one probe at a time), rate limiting, lease and crash recovery, two workers never sharing a job, SSRF blocked at send time, replay
- **API:** authentication, tenant isolation on every resource, endpoint management, publishing with fan-out and filters, idempotency including payload key order and per-tenant keys, pagination
- **Concurrency with real threads:** eight identical publish requests at the same instant create exactly one event; a worker loop with several threads delivers twelve events exactly once
- **Migrations:** the migrated schema has no drift from the models, upgrade and downgrade work
- **Reserved words:** no table or column name is a reserved word in PostgreSQL or MySQL
- Key guarantees were also checked by deliberately breaking the code (removing the lease, the tenant check, redirect handling, back-off growth, idempotency race handling, a migration column, the signature) and confirming a test fails each time

By default the tests use in-memory SQLite. To run the same suite against PostgreSQL set `WHP_TEST_DATABASE_URL`, for example `postgresql+psycopg://user:pass@localhost:5434/webhooks_test`; CI runs both.

## Design decisions
- **The queue is the database.** A `deliveries` table with `FOR UPDATE SKIP LOCKED` instead of a separate broker such as Redis or RabbitMQ. The event and its jobs are written in one transaction, so nothing can be accepted and then lost; workers coordinate through the database; there is one component fewer to run and test. The cost is throughput: it suits thousands of deliveries per second, not millions (see limitations).
- **At-least-once, with stable ids.** A job is reserved with a lease; a crashed worker's job reappears. A receiver may therefore see a message twice, which is why `webhook-id` is the same on every retry.
- **No transaction during the network call.** Holding a database transaction open while waiting for a slow customer server would exhaust connections.
- **Rate limiting counts recorded attempts.** It works across workers without extra infrastructure, at the price of being approximate when many workers decide at the same moment.
- **Circuit breaker counts availability failures only** (network errors, timeouts, 5xx, 408, 429). A `400` or `404` means the server is alive, so it does not open the circuit.
- **Metrics come from the database**, because the API and the workers are separate processes and in-memory counters would each show half the picture.
- **Time is always UTC and timezone-aware**, enforced by a custom column type, so SQLite and PostgreSQL behave identically.

## Known limitations
- **Signing secrets are stored in plain text** because the platform needs them to sign requests. A production deployment should encrypt them at rest (for example with a key management service).
- The database as a queue limits throughput. For very high volume, move the queue to a broker and keep the database as the record.
- The rate limiter is a check-then-act, so it can overshoot slightly under heavy parallelism.
- The address check resolves the host name and then the HTTP client resolves it again, leaving a small DNS-rebinding window; closing it needs connecting to the checked IP directly.
- There is no cleanup of old events and attempts (a retention job would be needed), no per-customer quotas, and no UI.
- One shared admin token protects the operator endpoints; real use would want per-operator accounts.
- `Retry-After` is honoured in seconds only, not as an HTTP date.
- A signing secret rotation takes effect immediately (no period where both secrets are valid).
