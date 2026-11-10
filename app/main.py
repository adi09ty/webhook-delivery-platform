"""The web application. Run it with   uvicorn app.main:app"""

from collections.abc import Callable
from datetime import datetime

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.clock import utcnow
from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory
from app.deps import AdminDep, SessionDep
from app.metrics import render_metrics
from app.routes import admin, deliveries, endpoints, events

DESCRIPTION = """
Reliable webhook delivery as a service.

Customers register **endpoints** (URLs), publish **events**, and the platform delivers each event to every
matching endpoint in the background: signed with HMAC, retried with back-off, logged attempt by attempt,
parked in a dead-letter queue when it keeps failing, and replayable once the receiver is fixed.
"""


def create_app(
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
    clock: Callable[[], datetime] = utcnow,
) -> FastAPI:
    settings = settings or get_settings()
    if session_factory is None:
        session_factory = make_session_factory(make_engine(settings.database_url))

    app = FastAPI(title="Webhook Delivery Platform", version="1.0.0", description=DESCRIPTION)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.clock = clock

    app.include_router(admin.router)
    for router in (endpoints.router, events.router, deliveries.router):
        app.include_router(router, prefix="/v1")

    @app.get("/health", tags=["ops"])
    def health(request: Request) -> Response:
        """Is the service up and able to reach its database?"""
        try:
            with request.app.state.session_factory() as session:
                session.execute(text("SELECT 1"))
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.get("/metrics", tags=["ops"], dependencies=[AdminDep], response_class=Response)
    def metrics(request: Request, session: SessionDep) -> Response:
        """Prometheus-format metrics. Needs the X-Admin-Token header."""
        body = render_metrics(session, request.app.state.clock())
        return Response(content=body, media_type="text/plain; version=0.0.4")

    return app


app = create_app()
