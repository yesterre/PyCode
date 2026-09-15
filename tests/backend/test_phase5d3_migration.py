from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool
from uuid import uuid4

from backend.app.config import load_environment


PREVIOUS_REVISION = "20260914_0006"
PHASE5D3_REVISION = "20260915_0007"


def _safe_test_database_url() -> str:
    load_environment()
    value = os.environ.get("TEST_DATABASE_URL")
    if not value:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    test_url = make_url(value)
    if (test_url.database or "").lower() != "pycode_test":
        pytest.fail("Phase 5D-3 migration tests require pycode_test", pytrace=False)
    development = os.environ.get("DATABASE_URL")
    if development and test_url == make_url(development):
        pytest.fail("TEST_DATABASE_URL must not equal DATABASE_URL", pytrace=False)
    return value


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.attributes["database_url"] = database_url
    return config


def test_phase5d3_migration_adds_nullable_lease_fields_and_partial_index():
    database_url = _safe_test_database_url()
    config = _config(database_url)
    command.downgrade(config, PREVIOUS_REVISION)
    engine = create_engine(database_url, poolclass=NullPool, hide_parameters=True)
    try:
        legacy_id = uuid4()
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO background_tasks "
                "(id, task_type, status, attempt_count, enqueue_attempt_count, "
                "created_at, started_at) VALUES "
                "(:id, 'health', 'running', 1, 1, now(), now())"
            ), {"id": legacy_id})
        before = {column["name"] for column in inspect(engine).get_columns("background_tasks")}
        command.upgrade(config, PHASE5D3_REVISION)
        inspector = inspect(engine)
        columns = {
            column["name"]: column
            for column in inspector.get_columns("background_tasks")
        }
        for name in {
            "execution_token", "lease_expires_at", "last_heartbeat_at", "worker_id",
        }:
            assert name not in before
            assert columns[name]["nullable"] is True
        indexes = {
            index["name"]: index
            for index in inspector.get_indexes("background_tasks")
        }
        stale = indexes["ix_background_tasks_running_lease"]
        assert stale["column_names"] == ["lease_expires_at", "id"]
        predicate = str(stale["dialect_options"]["postgresql_where"])
        assert "status" in predicate
        assert "running" in predicate
        with engine.connect() as connection:
            legacy = connection.execute(text(
                "SELECT execution_token, lease_expires_at, last_heartbeat_at, "
                "worker_id FROM background_tasks WHERE id=:id"
            ), {"id": legacy_id}).one()
        assert tuple(legacy) == (None, None, None, None)
        command.downgrade(config, PREVIOUS_REVISION)
        assert {
            column["name"] for column in inspect(engine).get_columns("background_tasks")
        } == before
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM background_tasks WHERE id=:id"),
                {"id": legacy_id},
            )
    finally:
        engine.dispose()
        command.upgrade(config, "head")
        cleanup = create_engine(database_url, poolclass=NullPool, hide_parameters=True)
        try:
            with cleanup.begin() as connection:
                connection.execute(
                    text("DELETE FROM background_tasks WHERE id=:id"),
                    {"id": legacy_id},
                )
        finally:
            cleanup.dispose()
