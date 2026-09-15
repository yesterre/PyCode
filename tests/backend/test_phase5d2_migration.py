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


PREVIOUS_REVISION = "20260914_0005"
PHASE5D2_REVISION = "20260914_0006"


def _safe_test_database_url() -> str:
    load_environment()
    value = os.environ.get("TEST_DATABASE_URL")
    if not value:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    test_url = make_url(value)
    if (test_url.database or "").lower() != "pycode_test":
        pytest.fail("Phase 5D-2 migration tests require pycode_test", pytrace=False)
    development = os.environ.get("DATABASE_URL")
    if development and test_url == make_url(development):
        pytest.fail("TEST_DATABASE_URL must not equal DATABASE_URL", pytrace=False)
    return value


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.attributes["database_url"] = database_url
    return config


def test_phase5d2_migration_adds_and_removes_core_started_at():
    database_url = _safe_test_database_url()
    config = _config(database_url)
    command.downgrade(config, PREVIOUS_REVISION)
    engine = create_engine(database_url, poolclass=NullPool, hide_parameters=True)
    try:
        before = {column["name"] for column in inspect(engine).get_columns("agent_runs")}
        assert "core_started_at" not in before
        command.upgrade(config, PHASE5D2_REVISION)
        columns = {
            column["name"]: column
            for column in inspect(engine).get_columns("agent_runs")
        }
        assert columns["core_started_at"]["nullable"] is True
        assert columns["core_started_at"]["type"].timezone is True
        command.downgrade(config, PREVIOUS_REVISION)
        assert {
            column["name"] for column in inspect(engine).get_columns("agent_runs")
        } == before
    finally:
        engine.dispose()
        command.upgrade(config, "head")
