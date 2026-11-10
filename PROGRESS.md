# Webhook Delivery Platform: Progress Tracker

A Python backend that receives events from a company's app and delivers them reliably to that company's customers' servers, with signed requests, retries, delivery logs, a dead-letter queue and replay.

## How this was built
The project was built end to end with AI assistance at the owner's request, with the plan to learn it afterwards.
`docs/LEARNING_GUIDE.md` is the study plan. Describe the project honestly when presenting it.

## Stack
Python 3.13 (use `py -3.13`, NOT plain `python`, which on this machine is Python 2.7), FastAPI, PostgreSQL, SQLAlchemy 2 + Alembic, httpx, pytest, ruff, Docker, GitHub Actions.
One design change from the original plan: the queue is the database (`FOR UPDATE SKIP LOCKED`) instead of Redis.

## Milestones
- [x] **M1: Foundation:** FastAPI app, models, migrations, API keys, tenants, endpoints, events
- [x] **M2: Delivery:** worker, HMAC signing, delivery attempts
- [x] **M3: Reliability:** retries with back-off, dead-letter queue, replay, idempotent publishing, leases
- [x] **M4: Protection:** rate limiting, circuit breaker, SSRF protection, metrics
- [x] **M5: Quality:** 122 tests, Docker Compose, CI, README, learning guide, demo receiver and walkthrough

## Verification status
- [x] 122 automated tests pass on SQLite, stable over repeated runs; about 96% coverage; ruff clean
- [x] Mutation checks: 8 deliberate breakages, each caught by a test
- [x] Real end-to-end run with the API, worker and demo receiver as separate processes on SQLite (14 checks pass)
- [ ] Docker Compose stack with real PostgreSQL (needs to be run on the owner's machine)
- [ ] GitHub Actions CI (runs after the first push)

## Next steps
1. Run the Docker stack and the walkthrough against PostgreSQL.
2. Publish to GitHub, then confirm CI is green.
3. Work through `docs/LEARNING_GUIDE.md`, including the "make it yours" exercises.
