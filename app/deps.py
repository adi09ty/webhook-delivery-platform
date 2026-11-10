"""Shared building blocks for the routes: database session, current customer, admin check."""

import hmac
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.models import Tenant
from app.security import authenticate_api_key


def get_session(request: Request) -> Iterator[Session]:
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_session)]


def get_current_tenant(
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> Tenant:
    """Identify the customer from the header   Authorization: Bearer whp_...   (401 if missing or wrong)."""
    unauthorized = HTTPException(
        status_code=401, detail="Missing or invalid API key", headers={"WWW-Authenticate": "Bearer"}
    )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise unauthorized
    api_key = authenticate_api_key(session, authorization[7:].strip())
    if api_key is None:
        raise unauthorized
    tenant = session.get(Tenant, api_key.tenant_id)
    if tenant is None:
        raise unauthorized
    return tenant


TenantDep = Annotated[Tenant, Depends(get_current_tenant)]


def require_admin(request: Request, x_admin_token: Annotated[str | None, Header()] = None) -> None:
    """Guard for /admin and /metrics. With no admin token configured, those endpoints are off."""
    configured = request.app.state.settings.admin_token
    if not configured:
        raise HTTPException(status_code=503, detail="Admin API is disabled: set WHP_ADMIN_TOKEN")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, configured):
        raise HTTPException(status_code=401, detail="Invalid admin token")


AdminDep = Depends(require_admin)
