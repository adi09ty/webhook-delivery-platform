# Learning guide: understand every part of this project

You asked for the project to be completed first and learned later. This is the map for that. Read the files in the
order below, run the code, break it on purpose, and answer the questions out loud. The goal: be able to explain every
file and rebuild any part of it without help.

**Before you show this project to anyone:** do the "make it yours" exercises at the end, and describe the project
truthfully (it was built with AI assistance and you then studied, extended and tested it yourself). Never claim
you wrote code you cannot explain or reproduce.

## Reading order

| Step | File | What to understand | Concept |
|---|---|---|---|
| 1 | `app/config.py` | Every setting and why it exists | Configuration from environment variables |
| 2 | `app/models.py` | Each table and column; `UTCDateTime`; the unique constraints and indexes | Database design, timezone safety, constraints as guarantees |
| 3 | `app/security.py`, `app/deps.py` | How an API key becomes a tenant; why only a hash is stored | Authentication, dependency injection, `compare_digest` |
| 4 | `app/routes/endpoints.py` | Tenant scoping (`_get_own_endpoint`), 404 vs 403 | Multi-tenancy, authorisation |
| 5 | `app/routes/events.py` | Publish: idempotency, the race handling, event + deliveries in one transaction | Idempotency, transactions, the outbox idea |
| 6 | `app/signing.py` | The exact signed string; verification; the timestamp | HMAC, replay attacks |
| 7 | `app/urls.py` | What SSRF is and what is blocked | Security |
| 8 | `app/retry.py`, `app/circuit.py` | Back-off with jitter; the three circuit states as a state machine | Resilience patterns |
| 9 | `app/delivery.py` | claim, prepare, send, record: the heart of the system | Queues with leases, `SKIP LOCKED`, locking, failure handling |
| 10 | `app/worker.py` | The loop, threads, graceful shutdown | Concurrency, background workers |
| 11 | `app/metrics.py` | Why metrics are read from the database | Observability |
| 12 | `alembic/versions/0001_initial_schema.py` | What a migration is; why it must not import app code | Schema migrations |
| 13 | `tests/` | Start with `test_circuit.py` and `test_signing.py`, then `test_delivery.py` | Testing: fakes, a controllable clock, real threads |

## Run things to see them work
1. `docker compose up --build`, then the walkthrough command from the README. Read the output next to `app/delivery.py`.
2. Open http://localhost:8000/docs and use the API by hand.
3. Run `pytest -v` and read the test names: they describe the behaviour in plain English.
4. In `docker compose logs -f worker`, publish an event while the receiver is in `error500` mode (see `demo/receiver.py`) and watch the retries.

## Break it on purpose (the best way to learn)
For each change, predict which test fails, then run `pytest` to check.
- In `app/delivery.py`, remove the line that sets `locked_until` in `claim_batch`.
- In `app/routes/endpoints.py`, remove the `tenant_id` condition in `_get_own_endpoint`.
- In `app/delivery.py`, change `follow_redirects=False` to `True`.
- In `app/retry.py`, make every retry wait the same time.
- In `app/routes/events.py`, rename `except IntegrityError` so the race is no longer handled.

## Interview questions this project prepares you for
**Reliability and queues**
- How do you make sure an event is never lost between "accepted" and "delivered"? (event and jobs in one transaction)
- What does at-least-once delivery mean, and how does a receiver cope with duplicates? (stable `webhook-id`)
- What is `FOR UPDATE SKIP LOCKED`, and why is it good for a queue?
- A worker crashes after claiming a job. What happens? (the lease expires)
- Why not keep a database transaction open during the HTTP call?
- Why exponential back-off, and what is jitter for?

**Idempotency and concurrency**
- What is an idempotency key? What happens with the same key and a different body? (409)
- Two identical requests arrive at the same instant. Who wins and how? (the unique constraint; the loser re-reads)
- How do two workers avoid sending the same delivery? How does the endpoint row lock prevent two workers from both becoming the "test request"?

**Security**
- How does HMAC signing prove a message is genuine? Why include a timestamp?
- Why is the API key stored as a hash but the signing secret in plain text?
- What is SSRF? Why check the address at registration and again at send time? Why not follow redirects?
- Why return 404 instead of 403 for another customer's resource?

**Design**
- Why a database as the queue and not Redis or Kafka? When would you switch? (throughput)
- How does the circuit breaker work, and why do 400s not count as failures?
- Why are metrics read from the database?
- What would you change to handle ten times the load?

**Python and tools**
- What does `Depends()` do in FastAPI? What is Pydantic validating?
- What is a migration and why is `alembic` better than `create_all` in production?
- How do you test code that depends on time? (an injected clock)
- Why does the test for threads use a file database and not in-memory SQLite?

## Make it yours: exercises to do before presenting
1. **Rewrite a module from scratch.** Delete `app/circuit.py` and `app/retry.py`, rewrite them from the tests alone, and make the tests pass.
2. **Add a feature.** For example: an `X-Webhook-Event-Type` header on deliveries; or a `GET /v1/endpoints/{id}/stats` endpoint (success rate, average duration); or deleting old attempts after 30 days. Write the tests first, in your own commits.
3. **Fix a limitation.** For example, encrypt endpoint secrets at rest, or parse `Retry-After` as an HTTP date.
4. **Explain it.** Record yourself explaining the five-step "How one delivery works" without looking at the code.
5. **Measure.** Publish 5,000 events and note how many deliveries per second one worker handles. Put the real numbers in the README. Never invent numbers.

## Things worth knowing about this codebase
- Tests use `FakeClock` so retries, cooldowns and leases are tested without waiting.
- `tests/conftest.py` can run the whole suite on PostgreSQL by setting `WHP_TEST_DATABASE_URL`.
- Migrations are generated from the models (`alembic revision --autogenerate`) and then reviewed by hand; a test fails if the migration and the models ever disagree.
