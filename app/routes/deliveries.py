"""The delivery log: what was sent where, what came back, and the replay button."""

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import func, select

from app.deps import SessionDep, TenantDep
from app.models import Delivery, DeliveryAttempt, DeliveryStatus, Endpoint
from app.routes.endpoints import requeue
from app.schemas import AttemptOut, DeliveryDetail, DeliveryOut, Page

router = APIRouter(prefix="/deliveries", tags=["deliveries"])


def _get_own_delivery(session: SessionDep, tenant_id: uuid.UUID, delivery_id: uuid.UUID) -> Delivery:
    delivery = session.scalar(select(Delivery).where(Delivery.id == delivery_id, Delivery.tenant_id == tenant_id))
    if delivery is None:
        raise HTTPException(status_code=404, detail="Delivery not found")
    return delivery


@router.get("", response_model=Page[DeliveryOut])
def list_deliveries(
    session: SessionDep,
    tenant: TenantDep,
    status: Annotated[str | None, Query(pattern="^(pending|succeeded|dead)$")] = None,
    endpoint_id: uuid.UUID | None = None,
    event_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[DeliveryOut]:
    conditions = [Delivery.tenant_id == tenant.id]
    if status:
        conditions.append(Delivery.status == status)
    if endpoint_id:
        conditions.append(Delivery.endpoint_id == endpoint_id)
    if event_id:
        conditions.append(Delivery.event_id == event_id)
    total = session.scalar(select(func.count()).select_from(Delivery).where(*conditions))
    rows = session.scalars(
        select(Delivery)
        .where(*conditions)
        .order_by(Delivery.created_at.desc(), Delivery.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return Page(items=[DeliveryOut.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/{delivery_id}", response_model=DeliveryDetail)
def get_delivery(delivery_id: uuid.UUID, session: SessionDep, tenant: TenantDep) -> DeliveryDetail:
    delivery = _get_own_delivery(session, tenant.id, delivery_id)
    attempts = session.scalars(
        select(DeliveryAttempt)
        .where(DeliveryAttempt.delivery_id == delivery.id)
        .order_by(DeliveryAttempt.started_at, DeliveryAttempt.attempt_number)
    ).all()
    return DeliveryDetail(
        **DeliveryOut.model_validate(delivery).model_dump(),
        attempts=[AttemptOut.model_validate(a) for a in attempts],
    )


@router.post("/{delivery_id}/replay", response_model=DeliveryOut)
def replay_delivery(delivery_id: uuid.UUID, request: Request, session: SessionDep, tenant: TenantDep) -> Delivery:
    """Send a finished (dead or succeeded) delivery again, with a fresh set of retries."""
    delivery = _get_own_delivery(session, tenant.id, delivery_id)
    if delivery.status == DeliveryStatus.PENDING:
        raise HTTPException(status_code=409, detail="Delivery is already waiting to be sent")
    endpoint = session.get(Endpoint, delivery.endpoint_id)
    if not endpoint.enabled:
        raise HTTPException(status_code=409, detail="Endpoint is disabled. Enable it before replaying.")
    requeue(delivery, request.app.state.clock())
    session.commit()
    return delivery
