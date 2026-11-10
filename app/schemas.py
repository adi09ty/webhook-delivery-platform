"""Request and response shapes of the API (Pydantic validates incoming data and shapes outgoing data)."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

EventTypeList = Annotated[list[str], Field(max_length=50)]


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


# ------------------------------------------------------------------ admin


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class TenantCreated(BaseModel):
    id: uuid.UUID
    name: str
    api_key_id: uuid.UUID
    api_key: str = Field(description="Shown only once. Store it safely.")


class ApiKeyCreate(BaseModel):
    name: str = Field(default="default", min_length=1, max_length=100)


class ApiKeyCreated(BaseModel):
    id: uuid.UUID
    name: str
    api_key: str = Field(description="Shown only once. Store it safely.")


# -------------------------------------------------------------- endpoints


def _check_event_types(values: list[str] | None) -> list[str] | None:
    if values is None:
        return values
    for value in values:
        if not value or len(value) > 100:
            raise ValueError("each event type must be 1 to 100 characters")
    return values


class EndpointCreate(BaseModel):
    url: str
    description: str | None = Field(default=None, max_length=255)
    event_types: EventTypeList = Field(
        default_factory=list, description="Only these event types are delivered. Empty means all."
    )
    rate_limit_per_minute: int | None = Field(
        default=None, ge=0, le=100_000, description="Maximum deliveries per minute. 0 means unlimited."
    )

    _validate_types = field_validator("event_types")(_check_event_types)


class EndpointUpdate(BaseModel):
    url: str | None = None
    description: str | None = Field(default=None, max_length=255)
    event_types: EventTypeList | None = None
    enabled: bool | None = None
    rate_limit_per_minute: int | None = Field(default=None, ge=0, le=100_000)

    _validate_types = field_validator("event_types")(_check_event_types)


class EndpointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    url: str
    description: str | None
    event_types: list[str]
    enabled: bool
    disabled_reason: str | None
    rate_limit_per_minute: int
    circuit_state: str
    consecutive_failures: int
    created_at: datetime


class EndpointWithSecret(EndpointOut):
    secret: str = Field(description="The signing secret. Shown only when created or rotated.")


# ----------------------------------------------------------------- events


class EventCreate(BaseModel):
    event_type: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:\-]+$")
    payload: dict[str, Any]


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_type: str
    payload: dict[str, Any]
    idempotency_key: str | None
    created_at: datetime
    deliveries_created: int | None = None


# -------------------------------------------------------------- deliveries


class AttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    attempt_number: int
    started_at: datetime
    duration_ms: int
    outcome: str
    response_status: int | None
    response_body: str | None
    error: str | None


class DeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_id: uuid.UUID
    endpoint_id: uuid.UUID
    status: str
    attempt_count: int
    next_attempt_at: datetime
    last_error: str | None
    last_status_code: int | None
    replay_count: int
    created_at: datetime
    completed_at: datetime | None


class DeliveryDetail(DeliveryOut):
    attempts: list[AttemptOut]


class ReplayResult(BaseModel):
    replayed: int
