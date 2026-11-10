"""Where a customer publishes events."""

import hashlib
import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.deps import SessionDep, TenantDep
from app.models import Delivery, Endpoint, Event
from app.schemas import DeliveryOut, EventCreate, EventOut, Page

router = APIRouter(prefix="/events", tags=["events"])


def _find_by_key(session: SessionDep, tenant_id: uuid.UUID, key: str) -> Event | None:
    return session.scalar(select(Event).where(Event.tenant_id == tenant_id, Event.idempotency_key == key))


def _as_replay(session: SessionDep, existing: Event, request_hash: str, response: Response) -> EventOut:
    """Same Idempotency-Key and same content: answer with the original event. Different content: refuse."""
    if existing.request_hash != request_hash:
        raise HTTPException(status_code=409, detail="Idempotency-Key was already used with a different request")
    response.status_code = 200
    response.headers["Idempotent-Replayed"] = "true"
    count = session.scalar(select(func.count()).select_from(Delivery).where(Delivery.event_id == existing.id))
    out = EventOut.model_validate(existing)
    out.deliveries_created = count
    return out


@router.post("", status_code=202, response_model=EventOut)
def publish_event(
    body: EventCreate,
    request: Request,
    response: Response,
    session: SessionDep,
    tenant: TenantDep,
    idempotency_key: Annotated[str | None, Header(max_length=128)] = None,
) -> EventOut:
    """Publish an event. It is saved together with one delivery job per matching endpoint, in one transaction,
    so an accepted event can never exist without its deliveries. Returns 202: delivery happens in the background.

    Send an Idempotency-Key header to make retries of this call safe.
    """
    settings = request.app.state.settings
    canonical = json.dumps({"type": body.event_type, "payload": body.payload}, sort_keys=True, separators=(",", ":"))
    if len(canonical.encode()) > settings.max_payload_bytes:
        raise HTTPException(status_code=413, detail=f"Payload is larger than {settings.max_payload_bytes} bytes")
    request_hash = hashlib.sha256(canonical.encode()).hexdigest()

    if idempotency_key:
        existing = _find_by_key(session, tenant.id, idempotency_key)
        if existing is not None:
            return _as_replay(session, existing, request_hash, response)

    now = request.app.state.clock()
    event = Event(
        tenant_id=tenant.id,
        event_type=body.event_type,
        payload=body.payload,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        created_at=now,
    )
    session.add(event)
    try:
        session.flush()
    except IntegrityError:
        # Two identical requests raced and the other one won. The unique constraint in the database decided it.
        session.rollback()
        existing = _find_by_key(session, tenant.id, idempotency_key) if idempotency_key else None
        if existing is None:
            raise
        return _as_replay(session, existing, request_hash, response)

    endpoints = session.scalars(
        select(Endpoint).where(Endpoint.tenant_id == tenant.id, Endpoint.enabled.is_(True))
    ).all()
    matching = [e for e in endpoints if not e.event_types or body.event_type in e.event_types]
    for endpoint in matching:
        session.add(
            Delivery(
                tenant_id=tenant.id,
                event_id=event.id,
                endpoint_id=endpoint.id,
                next_attempt_at=now,
                created_at=now,
            )
        )
    session.commit()

    out = EventOut.model_validate(event)
    out.deliveries_created = len(matching)
    return out


@router.get("", response_model=Page[EventOut])
def list_events(
    session: SessionDep,
    tenant: TenantDep,
    event_type: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[EventOut]:
    conditions = [Event.tenant_id == tenant.id]
    if event_type:
        conditions.append(Event.event_type == event_type)
    total = session.scalar(select(func.count()).select_from(Event).where(*conditions))
    rows = session.scalars(
        select(Event).where(*conditions).order_by(Event.created_at.desc(), Event.id).limit(limit).offset(offset)
    ).all()
    return Page(items=[EventOut.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/{event_id}", response_model=EventOut)
def get_event(event_id: uuid.UUID, session: SessionDep, tenant: TenantDep) -> Event:
    event = session.scalar(select(Event).where(Event.id == event_id, Event.tenant_id == tenant.id))
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


@router.get("/{event_id}/deliveries", response_model=list[DeliveryOut])
def list_event_deliveries(event_id: uuid.UUID, session: SessionDep, tenant: TenantDep) -> list[Delivery]:
    if session.scalar(select(Event.id).where(Event.id == event_id, Event.tenant_id == tenant.id)) is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return list(
        session.scalars(
            select(Delivery)
            .where(Delivery.event_id == event_id, Delivery.tenant_id == tenant.id)
            .order_by(Delivery.created_at)
        )
    )
