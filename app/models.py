"""Database tables.

Tenant     a customer of the platform (a company that publishes events)
ApiKey     a secret key that lets a tenant call the API
Endpoint   a URL the tenant wants events delivered to, with its signing secret and health state
Event      something that happened ("payment.completed" plus data)
Delivery   the job "send this event to that endpoint" (this table is the queue)
DeliveryAttempt  one try of a delivery: what we sent, what came back, how long it took
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.db import Base


class UTCDateTime(TypeDecorator):
    """A timestamp that is always timezone-aware UTC in Python.

    PostgreSQL keeps the timezone, SQLite throws it away. This type makes both behave the same, and refuses
    naive datetimes so a time can never be silently misread as local time.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime: use timezone-aware UTC datetimes")
        return value.astimezone(UTC)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class DeliveryStatus:
    PENDING = "pending"  # waiting for a first try, or waiting to be retried
    SUCCEEDED = "succeeded"
    DEAD = "dead"  # gave up: this is the dead-letter queue


class CircuitState:
    CLOSED = "closed"  # healthy: send normally
    OPEN = "open"  # too many failures: stop sending for a while
    HALF_OPEN = "half_open"  # cooling-off over: allow one test request


class AttemptOutcome:
    SUCCESS = "success"
    HTTP_ERROR = "http_error"
    NETWORK_ERROR = "network_error"
    BLOCKED = "blocked"  # refused before sending (unsafe address)


def _now() -> datetime:
    return datetime.now(UTC)


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    # Only a hash of the key is stored. The key itself is shown once, when it is created.
    key_prefix: Mapped[str] = mapped_column(String(16), index=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Endpoint(Base):
    __tablename__ = "endpoints"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(2048))
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The secret is needed in clear to sign every request, so it cannot be hashed like an API key.
    secret: Mapped[str] = mapped_column(String(128))
    # Which event types this endpoint wants. An empty list means "all of them".
    event_types: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(default=True)
    disabled_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=60)  # 0 means unlimited
    # Circuit breaker state
    circuit_state: Mapped[str] = mapped_column(String(12), default=CircuitState.CLOSED)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    circuit_opened_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    probe_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key", name="uk_events_idempotency"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    # Lets a caller retry "publish" safely: the same key returns the first event instead of creating a second.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint("event_id", "endpoint_id", name="uk_delivery_event_endpoint"),
        # The worker's "what is due now?" query uses this index.
        Index("ix_deliveries_due", "status", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    endpoint_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("endpoints.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(12), default=DeliveryStatus.PENDING)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime)
    # A worker "reserves" a delivery by setting this to a future time. If the worker crashes, the reservation
    # runs out and another worker takes the job (at-least-once delivery).
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    replay_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class DeliveryAttempt(Base):
    __tablename__ = "delivery_attempts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    delivery_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("deliveries.id", ondelete="CASCADE"), index=True)
    endpoint_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("endpoints.id", ondelete="CASCADE"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    duration_ms: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(16))
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
