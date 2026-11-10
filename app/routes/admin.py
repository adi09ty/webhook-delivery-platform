"""Operator-only endpoints: create customers (tenants) and their API keys. Protected by X-Admin-Token."""

import uuid

from fastapi import APIRouter, HTTPException, Request, Response

from app.deps import AdminDep, SessionDep
from app.models import ApiKey, Tenant
from app.schemas import ApiKeyCreate, ApiKeyCreated, TenantCreate, TenantCreated
from app.security import generate_api_key

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[AdminDep])


@router.post("/tenants", status_code=201, response_model=TenantCreated)
def create_tenant(body: TenantCreate, request: Request, session: SessionDep) -> TenantCreated:
    now = request.app.state.clock()
    tenant = Tenant(name=body.name.strip(), created_at=now)
    session.add(tenant)
    session.flush()
    key, prefix, key_hash = generate_api_key()
    api_key = ApiKey(tenant_id=tenant.id, name="default", key_prefix=prefix, key_hash=key_hash, created_at=now)
    session.add(api_key)
    session.commit()
    return TenantCreated(id=tenant.id, name=tenant.name, api_key_id=api_key.id, api_key=key)


@router.post("/tenants/{tenant_id}/api-keys", status_code=201, response_model=ApiKeyCreated)
def create_api_key(tenant_id: uuid.UUID, body: ApiKeyCreate, request: Request, session: SessionDep) -> ApiKeyCreated:
    if session.get(Tenant, tenant_id) is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    key, prefix, key_hash = generate_api_key()
    api_key = ApiKey(
        tenant_id=tenant_id,
        name=body.name,
        key_prefix=prefix,
        key_hash=key_hash,
        created_at=request.app.state.clock(),
    )
    session.add(api_key)
    session.commit()
    return ApiKeyCreated(id=api_key.id, name=api_key.name, api_key=key)


@router.delete("/api-keys/{api_key_id}", status_code=204)
def revoke_api_key(api_key_id: uuid.UUID, request: Request, session: SessionDep) -> Response:
    api_key = session.get(ApiKey, api_key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    if api_key.revoked_at is None:
        api_key.revoked_at = request.app.state.clock()
        session.commit()
    return Response(status_code=204)
