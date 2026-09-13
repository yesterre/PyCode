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


PHASE4_REVISION = "20260910_0002"
PHASE5A_REVISION = "20260911_0003"


def _safe_test_database_url() -> str:
    load_environment()
    value = os.environ.get("TEST_DATABASE_URL")
    if not value:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    try:
        test_url = make_url(value)
    except Exception:
        pytest.fail("TEST_DATABASE_URL is not a valid SQLAlchemy URL", pytrace=False)
    if (test_url.database or "").lower() != "pycode_test":
        pytest.fail("Phase 5A migration tests require the pycode_test database", pytrace=False)
    development = os.environ.get("DATABASE_URL")
    if development and test_url == make_url(development):
        pytest.fail("TEST_DATABASE_URL must not equal DATABASE_URL", pytrace=False)
    return value


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.attributes["database_url"] = database_url
    return config


def test_phase5a_migration_adds_and_removes_only_background_tasks():
    database_url = _safe_test_database_url()
    config = _config(database_url)
    command.downgrade(config, PHASE4_REVISION)
    engine = create_engine(database_url, poolclass=NullPool, hide_parameters=True)
    try:
        before = set(inspect(engine).get_table_names())
        assert "background_tasks" not in before

        command.upgrade(config, PHASE5A_REVISION)
        inspector = inspect(engine)
        after = set(inspector.get_table_names())
        assert after - before == {"background_tasks"}
        columns = {column["name"] for column in inspector.get_columns("background_tasks")}
        assert columns == {
            "id", "task_type", "status", "project_id", "snapshot_id", "agent_run_id",
            "attempt_count", "error_code", "error_message", "created_at", "started_at",
            "finished_at",
        }
        foreign_keys = {
            tuple(foreign_key["constrained_columns"]): foreign_key["referred_table"]
            for foreign_key in inspector.get_foreign_keys("background_tasks")
        }
        assert foreign_keys == {
            ("project_id",): "projects",
            ("snapshot_id",): "project_snapshots",
            ("agent_run_id",): "agent_runs",
        }

        command.downgrade(config, PHASE4_REVISION)
        assert set(inspect(engine).get_table_names()) == before
    finally:
        engine.dispose()
        command.upgrade(config, "head")
