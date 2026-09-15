from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from backend.app.config import load_environment


PREVIOUS_REVISION = "20260914_0004"
PHASE5D4B_REVISION = "20260914_0005"


def _safe_test_database_url() -> str:
    load_environment()
    value = os.environ.get("TEST_DATABASE_URL")
    if not value:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    test_url = make_url(value)
    if (test_url.database or "").lower() != "pycode_test":
        pytest.fail("Phase 5D-4B migration tests require the pycode_test database", pytrace=False)
    development = os.environ.get("DATABASE_URL")
    if development and test_url == make_url(development):
        pytest.fail("TEST_DATABASE_URL must not equal DATABASE_URL", pytrace=False)
    return value


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.attributes["database_url"] = database_url
    return config


def test_phase5d4b_migration_adds_enqueue_metadata_and_scanner_index():
    database_url = _safe_test_database_url()
    config = _config(database_url)
    command.downgrade(config, PREVIOUS_REVISION)
    engine = create_engine(database_url, poolclass=NullPool, hide_parameters=True)
    try:
        before = inspect(engine)
        before_columns = {column["name"] for column in before.get_columns("background_tasks")}
        before_indexes = {index["name"] for index in before.get_indexes("background_tasks")}

        command.upgrade(config, PHASE5D4B_REVISION)
        after = inspect(engine)
        after_columns = {column["name"] for column in after.get_columns("background_tasks")}
        indexes = {index["name"]: index for index in after.get_indexes("background_tasks")}
        checks = {check["name"] for check in after.get_check_constraints("background_tasks")}

        assert after_columns - before_columns == {
            "enqueue_attempt_count",
            "last_enqueue_attempt_at",
            "enqueue_confirmed_at",
            "next_enqueue_attempt_at",
        }
        scanner_index = indexes["ix_background_tasks_queued_unconfirmed_next"]
        assert scanner_index["unique"] is False
        predicate = str(scanner_index["dialect_options"]["postgresql_where"])
        assert "queued" in predicate
        assert "enqueue_confirmed_at IS NULL" in predicate
        assert "ck_background_tasks_background_task_enqueue_attempt_count" in checks

        command.downgrade(config, PREVIOUS_REVISION)
        downgraded = inspect(engine)
        assert {column["name"] for column in downgraded.get_columns("background_tasks")} == before_columns
        assert {index["name"] for index in downgraded.get_indexes("background_tasks")} == before_indexes
    finally:
        engine.dispose()
        command.upgrade(config, "head")
