"""Database engine and session setup."""

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    """Parent class of every table model."""


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        kwargs: dict = {"connect_args": {"check_same_thread": False, "timeout": 30}}
        if url in ("sqlite://", "sqlite:///:memory:") or ":memory:" in url:
            # An in-memory SQLite database lives inside one connection, so every session must share it.
            kwargs["poolclass"] = StaticPool
        engine = create_engine(url, **kwargs)

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _record):
            # SQLite ignores foreign keys unless asked; PostgreSQL always enforces them.
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine
    return create_engine(url, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False: objects stay readable after commit, so a route can commit and then return them.
    return sessionmaker(engine, expire_on_commit=False)
