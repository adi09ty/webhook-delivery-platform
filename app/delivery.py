"""The heart of the platform: pick due deliveries, send them, and record what happened.

A delivery goes through three short database steps with the slow network call in the middle:

    1. prepare  lock the endpoint, run the safety checks (disabled? rate limit? circuit open?), build the request
    2. send     make the HTTP call (NO database transaction is held open during the network call)
    3. record   save the attempt, then decide: success, retry later, or dead

A worker "claims" deliveries by reserving them for `lease_seconds`. If the worker dies halfway, the reservation
expires and another worker picks the delivery up again. That gives "at least once" delivery, which is why every
request carries a stable webhook-id: receivers can safely ignore a repeat.
"""

import json
import logging
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app import circuit, signing
from app.clock import utcnow
from app.config import Settings
from app.models import (
    AttemptOutcome,
    CircuitState,
    Delivery,
    DeliveryAttempt,
    DeliveryStatus,
    Endpoint,
    Event,
)
from app.retry import next_retry_delay
from app.urls import UnsafeTargetError, ensure_public_host

log = logging.getLogger(__name__)

USER_AGENT = "webhook-delivery-platform/1.0"

# What process() reports, mostly useful for tests and logs.
SUCCEEDED = "succeeded"
RETRY_SCHEDULED = "retry_scheduled"
DEAD = "dead"
DEFERRED_CIRCUIT = "deferred_circuit"
DEFERRED_RATE_LIMIT = "deferred_rate_limit"
SKIPPED = "skipped"


@dataclass
class SendResult:
    outcome: str
    status_code: int | None
    body_snippet: str | None
    error: str | None
    duration_ms: int
    retry_after_seconds: int | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome == AttemptOutcome.SUCCESS

    @property
    def counts_against_circuit(self) -> bool:
        """Availability problems trip the breaker. A 400 or 404 means the server is alive, so it does not."""
        if self.outcome == AttemptOutcome.NETWORK_ERROR:
            return True
        if self.outcome == AttemptOutcome.HTTP_ERROR and self.status_code is not None:
            return self.status_code >= 500 or self.status_code in (408, 429)
        return False


@dataclass
class _Prepared:
    endpoint_id: uuid.UUID
    url: str
    secret: str
    event_id: str
    body: bytes
    attempt_number: int
    now: datetime


def build_body(event: Event) -> bytes:
    """The exact bytes we sign and send. Sorted keys and no spaces, so the bytes are the same every time."""
    envelope = {
        "id": str(event.id),
        "type": event.event_type,
        "created_at": event.created_at.isoformat(),
        "data": event.payload,
    }
    return json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode()


def _parse_retry_after(value: str | None) -> int | None:
    """Retry-After may be seconds or a date; only the seconds form is honoured."""
    if value is None:
        return None
    try:
        seconds = int(value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


class DeliveryService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        http_client: httpx.Client,
        *,
        clock: Callable[[], datetime] = utcnow,
        rng: random.Random | None = None,
    ):
        self._session_factory = session_factory
        self._settings = settings
        self._http = http_client
        self._clock = clock
        self._rng = rng or random.Random()

    # ---------------------------------------------------------------- claiming

    def claim_batch(self, limit: int) -> list[uuid.UUID]:
        """Reserve up to `limit` due deliveries for this worker and return their ids.

        FOR UPDATE SKIP LOCKED means: if another worker is claiming at the same moment, skip the rows it has
        locked instead of waiting. Two workers therefore never get the same delivery.
        """
        with self._session_factory() as session, session.begin():
            now = self._clock()
            due = session.scalars(
                select(Delivery)
                .where(
                    Delivery.status == DeliveryStatus.PENDING,
                    Delivery.next_attempt_at <= now,
                    or_(Delivery.locked_until.is_(None), Delivery.locked_until < now),
                )
                .order_by(Delivery.next_attempt_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()
            for delivery in due:
                delivery.locked_until = now + timedelta(seconds=self._settings.lease_seconds)
            return [delivery.id for delivery in due]

    # -------------------------------------------------------------- processing

    def process(self, delivery_id: uuid.UUID) -> str:
        outcome, prepared = self._prepare(delivery_id)
        if prepared is None:
            return outcome
        result = self._send(prepared)
        return self._record(delivery_id, prepared, result)

    def _prepare(self, delivery_id: uuid.UUID) -> tuple[str, _Prepared | None]:
        settings = self._settings
        with self._session_factory() as session, session.begin():
            delivery = session.get(Delivery, delivery_id)
            if delivery is None or delivery.status != DeliveryStatus.PENDING:
                return SKIPPED, None
            # Lock the endpoint row so two workers cannot decide about the same endpoint at once.
            endpoint = session.get(Endpoint, delivery.endpoint_id, with_for_update=True)
            event = session.get(Event, delivery.event_id)
            now = self._clock()

            if not endpoint.enabled:
                self._mark_dead(delivery, now, "Endpoint is disabled")
                return DEAD, None

            # Rate limit first: it changes no state, so a deferral here leaves the circuit untouched.
            if endpoint.rate_limit_per_minute > 0 and self._over_rate_limit(session, endpoint, now):
                delivery.next_attempt_at = now + timedelta(seconds=settings.rate_limit_defer_seconds)
                delivery.locked_until = None
                return DEFERRED_RATE_LIMIT, None

            decision = circuit.check(
                endpoint,
                now,
                cooldown_seconds=settings.circuit_cooldown_seconds,
                probe_window_seconds=settings.circuit_probe_window_seconds,
            )
            if not decision.allowed:
                delivery.next_attempt_at = decision.retry_at
                delivery.locked_until = None
                return DEFERRED_CIRCUIT, None

            return "ready", _Prepared(
                endpoint_id=endpoint.id,
                url=endpoint.url,
                secret=endpoint.secret,
                event_id=str(event.id),
                body=build_body(event),
                attempt_number=delivery.attempt_count + 1,
                now=now,
            )

    @staticmethod
    def _over_rate_limit(session: Session, endpoint: Endpoint, now: datetime) -> bool:
        recent = session.scalar(
            select(func.count())
            .select_from(DeliveryAttempt)
            .where(
                DeliveryAttempt.endpoint_id == endpoint.id,
                DeliveryAttempt.started_at >= now - timedelta(seconds=60),
            )
        )
        return recent >= endpoint.rate_limit_per_minute

    def _send(self, prepared: _Prepared) -> SendResult:
        started = time.monotonic()
        try:
            if not self._settings.allow_private_targets:
                # Checked again at send time: the host name may point somewhere else than when it was registered.
                parts = urlsplit(prepared.url)
                ensure_public_host(parts.hostname, parts.port)

            headers = {
                "content-type": "application/json",
                "user-agent": USER_AGENT,
                "x-webhook-attempt": str(prepared.attempt_number),
                **signing.build_signature_headers(
                    prepared.secret, prepared.event_id, int(prepared.now.timestamp()), prepared.body
                ),
            }
            with self._http.stream(
                "POST",
                prepared.url,
                content=prepared.body,
                headers=headers,
                timeout=self._settings.request_timeout_seconds,
                follow_redirects=False,  # a redirect could lead somewhere unsafe, so it counts as a failure
            ) as response:
                snippet = self._read_snippet(response)
                status = response.status_code
                retry_after = _parse_retry_after(response.headers.get("retry-after")) if status in (429, 503) else None
            duration_ms = int((time.monotonic() - started) * 1000)
            if 200 <= status < 300:
                return SendResult(AttemptOutcome.SUCCESS, status, snippet, None, duration_ms)
            return SendResult(AttemptOutcome.HTTP_ERROR, status, snippet, f"HTTP {status}", duration_ms, retry_after)
        except UnsafeTargetError as exc:
            return SendResult(AttemptOutcome.BLOCKED, None, None, str(exc)[:1000], self._elapsed_ms(started))
        except httpx.HTTPError as exc:
            message = f"{type(exc).__name__}: {exc}"[:1000]
            return SendResult(AttemptOutcome.NETWORK_ERROR, None, None, message, self._elapsed_ms(started))

    def _read_snippet(self, response: httpx.Response) -> str:
        limit = self._settings.max_response_snippet
        data = bytearray()
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) >= limit:
                break  # never read an unbounded answer into memory
        return bytes(data[:limit]).decode("utf-8", errors="replace")

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    def _record(self, delivery_id: uuid.UUID, prepared: _Prepared, result: SendResult) -> str:
        settings = self._settings
        with self._session_factory() as session, session.begin():
            delivery = session.get(Delivery, delivery_id, with_for_update=True)
            endpoint = session.get(Endpoint, prepared.endpoint_id, with_for_update=True)
            now = self._clock()

            session.add(
                DeliveryAttempt(
                    delivery_id=delivery.id,
                    endpoint_id=endpoint.id,
                    attempt_number=prepared.attempt_number,
                    started_at=prepared.now,
                    duration_ms=result.duration_ms,
                    outcome=result.outcome,
                    response_status=result.status_code,
                    response_body=result.body_snippet,
                    error=result.error,
                )
            )
            delivery.attempt_count = prepared.attempt_number
            delivery.last_status_code = result.status_code
            delivery.locked_until = None

            if result.succeeded:
                delivery.status = DeliveryStatus.SUCCEEDED
                delivery.completed_at = now
                delivery.last_error = None
                circuit.on_success(endpoint)
                return SUCCEEDED

            delivery.last_error = result.error

            if result.counts_against_circuit:
                circuit.on_failure(endpoint, now, threshold=settings.circuit_failure_threshold)
            elif endpoint.circuit_state == CircuitState.HALF_OPEN:
                endpoint.probe_started_at = None  # the test request got an answer; let the next one test again

            if result.status_code == 410:
                # "Gone": the receiver says this URL will never accept webhooks again. Stop and switch it off.
                self._mark_dead(delivery, now, "Receiver answered 410 Gone")
                endpoint.enabled = False
                endpoint.disabled_reason = "Receiver answered 410 Gone"
                return DEAD

            delay = next_retry_delay(
                prepared.attempt_number, settings.retry_delays_seconds, settings.retry_jitter, self._rng
            )
            if delay is None:
                self._mark_dead(delivery, now, result.error)
                return DEAD
            if result.retry_after_seconds is not None:
                delay = max(delay, min(result.retry_after_seconds, settings.max_retry_after_seconds))
            delivery.next_attempt_at = now + timedelta(seconds=delay)
            return RETRY_SCHEDULED

    @staticmethod
    def _mark_dead(delivery: Delivery, now: datetime, error: str | None) -> None:
        delivery.status = DeliveryStatus.DEAD
        delivery.completed_at = now
        delivery.locked_until = None
        delivery.last_error = error
