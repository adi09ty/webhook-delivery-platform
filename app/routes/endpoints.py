"""Where a customer says "send my events to this URL"."""

import uuid

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import select

from app.deps import SessionDep, TenantDep
from app.models import CircuitState, Delivery, DeliveryStatus, Endpoint
from app.schemas import (
    EndpointCreate,
    EndpointOut,
    EndpointUpdate,
    EndpointWithSecret,
    ReplayResult,
)
from app.security import generate_signing_secret
from app.urls import validate_target_url

router = APIRouter(prefix="/endpoints", tags=["endpoints"])


def _checked_url(request: Request, url: str) -> str:
    try:
        return validate_target_url(url, allow_private=request.app.state.settings.allow_private_targets)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid endpoint URL: {exc}") from exc


def _get_own_endpoint(session: SessionDep, tenant_id: uuid.UUID, endpoint_id: uuid.UUID) -> Endpoint:
    """Always filter by tenant: another customer's endpoint must look like it does not exist (404, not 403)."""
    endpoint = session.scalar(select(Endpoint).where(Endpoint.id == endpoint_id, Endpoint.tenant_id == tenant_id))
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    return endpoint


@router.post("", status_code=201, response_model=EndpointWithSecret)
def create_endpoint(body: EndpointCreate, request: Request, session: SessionDep, tenant: TenantDep) -> Endpoint:
    settings = request.app.state.settings
    endpoint = Endpoint(
        tenant_id=tenant.id,
        url=_checked_url(request, body.url),
        description=body.description,
        secret=generate_signing_secret(),
        event_types=body.event_types,
        rate_limit_per_minute=(
            settings.default_rate_limit_per_minute if body.rate_limit_per_minute is None else body.rate_limit_per_minute
        ),
        created_at=request.app.state.clock(),
    )
    session.add(endpoint)
    session.commit()
    return endpoint


@router.get("", response_model=list[EndpointOut])
def list_endpoints(session: SessionDep, tenant: TenantDep) -> list[Endpoint]:
    return list(session.scalars(select(Endpoint).where(Endpoint.tenant_id == tenant.id).order_by(Endpoint.created_at)))


@router.get("/{endpoint_id}", response_model=EndpointOut)
def get_endpoint(endpoint_id: uuid.UUID, session: SessionDep, tenant: TenantDep) -> Endpoint:
    return _get_own_endpoint(session, tenant.id, endpoint_id)


@router.patch("/{endpoint_id}", response_model=EndpointOut)
def update_endpoint(
    endpoint_id: uuid.UUID, body: EndpointUpdate, request: Request, session: SessionDep, tenant: TenantDep
) -> Endpoint:
    endpoint = _get_own_endpoint(session, tenant.id, endpoint_id)
    changes = body.model_dump(exclude_unset=True)
    if "url" in changes and changes["url"] is not None:
        endpoint.url = _checked_url(request, changes["url"])
    if "description" in changes:
        endpoint.description = changes["description"]
    if changes.get("event_types") is not None:
        endpoint.event_types = changes["event_types"]
    if changes.get("rate_limit_per_minute") is not None:
        endpoint.rate_limit_per_minute = changes["rate_limit_per_minute"]
    if changes.get("enabled") is not None:
        endpoint.enabled = changes["enabled"]
        if endpoint.enabled:
            # Switching an endpoint back on is a fresh start for its health record.
            endpoint.disabled_reason = None
            endpoint.circuit_state = CircuitState.CLOSED
            endpoint.consecutive_failures = 0
            endpoint.circuit_opened_at = None
            endpoint.probe_started_at = None
    session.commit()
    return endpoint


@router.delete("/{endpoint_id}", status_code=204)
def delete_endpoint(endpoint_id: uuid.UUID, session: SessionDep, tenant: TenantDep) -> Response:
    endpoint = _get_own_endpoint(session, tenant.id, endpoint_id)
    session.delete(endpoint)  # its deliveries and attempts go with it (ON DELETE CASCADE in the database)
    session.commit()
    return Response(status_code=204)


@router.post("/{endpoint_id}/rotate-secret", response_model=EndpointWithSecret)
def rotate_secret(endpoint_id: uuid.UUID, session: SessionDep, tenant: TenantDep) -> Endpoint:
    endpoint = _get_own_endpoint(session, tenant.id, endpoint_id)
    endpoint.secret = generate_signing_secret()
    session.commit()
    return endpoint


@router.post("/{endpoint_id}/replay-dead", response_model=ReplayResult)
def replay_dead_deliveries(
    endpoint_id: uuid.UUID, request: Request, session: SessionDep, tenant: TenantDep
) -> ReplayResult:
    """Put every dead delivery of this endpoint back in the queue (use after fixing the receiving server)."""
    endpoint = _get_own_endpoint(session, tenant.id, endpoint_id)
    if not endpoint.enabled:
        raise HTTPException(status_code=409, detail="Endpoint is disabled. Enable it before replaying.")
    now = request.app.state.clock()
    dead = session.scalars(
        select(Delivery).where(Delivery.endpoint_id == endpoint.id, Delivery.status == DeliveryStatus.DEAD)
    ).all()
    for delivery in dead:
        requeue(delivery, now)
    session.commit()
    return ReplayResult(replayed=len(dead))


def requeue(delivery: Delivery, now) -> None:
    """Reset a finished delivery so the worker treats it as new. Its attempt history is kept."""
    delivery.status = DeliveryStatus.PENDING
    delivery.attempt_count = 0
    delivery.next_attempt_at = now
    delivery.locked_until = None
    delivery.completed_at = None
    delivery.last_error = None
    delivery.last_status_code = None
    delivery.replay_count += 1
