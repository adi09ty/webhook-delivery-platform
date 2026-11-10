"""The migrations must build exactly the schema the models describe, otherwise production and tests disagree."""

from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from alembic import command
from app.db import Base

ROOT = Path(__file__).resolve().parent.parent


def alembic_config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture
def migrated_url(tmp_path):
    url = f"sqlite:///{(tmp_path / 'migrated.db').as_posix()}"
    command.upgrade(alembic_config(url), "head")
    return url


def test_there_is_exactly_one_migration_head():
    heads = ScriptDirectory.from_config(alembic_config("sqlite://")).get_heads()
    assert len(heads) == 1


def test_upgrading_creates_every_table(migrated_url):
    tables = set(inspect(create_engine(migrated_url)).get_table_names())
    assert set(Base.metadata.tables) <= tables


def test_the_migrated_schema_has_no_drift_from_the_models(migrated_url):
    engine = create_engine(migrated_url)
    with engine.connect() as connection:
        differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert differences == [], f"models and migrations disagree: {differences}"


def test_downgrading_removes_everything(migrated_url):
    command.downgrade(alembic_config(migrated_url), "base")
    tables = set(inspect(create_engine(migrated_url)).get_table_names())
    assert not set(Base.metadata.tables) & tables
