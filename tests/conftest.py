"""Shared test setup.

By default the tests run on an in-memory SQLite database. Set WHP_TEST_DATABASE_URL to a PostgreSQL URL to run the
very same tests against PostgreSQL (CI does this), where FOR UPDATE SKIP LOCKED and real locking are exercised.
"""

import os
import random

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Base, make_engine, make_session_factory
from app.delivery import DeliveryService
from app.main import create_app
from tests.helpers import ADMIN_TOKEN, FakeClock

TEST_DATABASE_URL = os.environ.get("WHP_TEST_DATABASE_URL", "sqlite://")


def make_settings(**overrides) -> Settings:
    values = {
        "database_url": TEST_DATABASE_URL,
        "admin_token": ADMIN_TOKEN,
        "allow_private_targets": True,
        "retry_delays_seconds": [10, 20, 40],  # 4 attempts in total
        "retry_jitter": 0.0,
        "circuit_failure_threshold": 3,
        "circuit_cooldown_seconds": 100,
        "circuit_probe_window_seconds": 30,
        "default_rate_limit_per_minute": 60,
        "rate_limit_defer_seconds": 5,
        "lease_seconds": 60,
        **overrides,
    }
    return Settings(_env_file=None, **values)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def engine():
    engine = make_engine(TEST_DATABASE_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def session_factory(engine):
    return make_session_factory(engine)


@pytest.fixture
def shared_database(tmp_path):
    """A database several threads can use at once (a SQLite file; the real PostgreSQL if one is configured).

    The default in-memory SQLite lives inside a single connection, which is not safe to share between threads.
    Returns (url, session_factory).
    """
    if TEST_DATABASE_URL.startswith("sqlite"):
        url = f"sqlite:///{(tmp_path / 'shared.db').as_posix()}"
    else:
        url = TEST_DATABASE_URL
    engine = make_engine(url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield url, make_session_factory(engine)
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def client(settings, session_factory, clock) -> TestClient:
    return TestClient(create_app(settings, session_factory, clock))


@pytest.fixture
def make_service(settings, session_factory, clock):
    """Build a DeliveryService whose HTTP calls go to a fake receiver instead of the internet."""

    def build(receiver, **setting_overrides) -> DeliveryService:
        used = make_settings(**setting_overrides) if setting_overrides else settings
        http = httpx.Client(transport=httpx.MockTransport(receiver))
        return DeliveryService(session_factory, used, http, clock=clock, rng=random.Random(1))

    return build
