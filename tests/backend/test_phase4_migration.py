from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from backend.app.config import load_environment
from backend.app.core.errors import BackendError
from backend.app.core.models import IndexSummary
from backend.app.repositories import SqlAlchemyUnitOfWork


PHASE3_REVISION = "20260910_0001"
PHASE4_REVISION = "20260910_0002"


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
        pytest.fail("Phase 4 migration tests require the pycode_test database", pytrace=False)
    development = os.environ.get("DATABASE_URL")
    if development:
        try:
            same_url = test_url == make_url(development)
        except Exception:
            same_url = value == development
        if same_url:
            pytest.fail("TEST_DATABASE_URL must not equal DATABASE_URL", pytrace=False)
    return value


def _alembic_config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.attributes["database_url"] = database_url
    return config


def _engine(database_url: str) -> Engine:
    return create_engine(database_url, poolclass=NullPool, hide_parameters=True)


def _truncate(engine: Engine) -> None:
    _safe_test_database_url()
    with engine.begin() as connection:
        connection.execute(text(
            "TRUNCATE TABLE trace_events, agent_runs, project_snapshots, projects CASCADE"
        ))


@pytest.fixture
def phase3_database():
    database_url = _safe_test_database_url()
    config = _alembic_config(database_url)
    # Never downgrade below the stable Phase 3 schema in this test module.
    command.downgrade(config, PHASE3_REVISION)
    command.upgrade(config, PHASE3_REVISION)
    engine = _engine(database_url)
    _truncate(engine)
    engine.dispose()
    try:
        yield database_url, config
    finally:
        # Leave the shared test database at head for the remaining backend suite.
        command.upgrade(config, "head")
        cleanup = _engine(database_url)
        try:
            _truncate(cleanup)
        finally:
            cleanup.dispose()


def test_phase3_history_backfills_and_survives_phase4_migration(phase3_database):
    database_url, config = phase3_database
    project_id, older_id, latest_id = uuid4(), uuid4(), uuid4()
    created_at = datetime(2026, 9, 9, tzinfo=timezone.utc)
    older_at, latest_at = created_at + timedelta(hours=1), created_at + timedelta(hours=2)
    baseline = {
        older_id: (".pclens/index.json", ".pclens/code_graph.json", 3, 8, 5),
        latest_id: (".pclens/index.json", ".pclens/code_graph.json", 4, 10, 7),
    }

    engine = _engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text(
                """
                INSERT INTO projects
                    (id, name, repo_url, branch, status, workspace_path,
                     last_error, created_at, updated_at)
                VALUES
                    (:id, 'Phase 3 Project', 'https://github.com/example/history.git',
                     NULL, 'ready', 'C:/managed/history/repo', NULL, :created, :updated)
                """
            ), {"id": project_id, "created": created_at, "updated": latest_at})
            for snapshot_id, commit_sha, updated_at, values in (
                (older_id, "a" * 40, older_at, baseline[older_id]),
                (latest_id, "b" * 40, latest_at, baseline[latest_id]),
            ):
                connection.execute(text(
                    """
                    INSERT INTO project_snapshots
                        (id, project_id, commit_sha, index_artifact_path,
                         graph_artifact_path, file_count, node_count, edge_count,
                         status, created_at, updated_at)
                    VALUES
                        (:id, :project_id, :commit_sha, :index_path, :graph_path,
                         :files, :nodes, :edges, 'ready', :created, :updated)
                    """
                ), {
                    "id": snapshot_id, "project_id": project_id,
                    "commit_sha": commit_sha, "index_path": values[0],
                    "graph_path": values[1], "files": values[2],
                    "nodes": values[3], "edges": values[4],
                    "created": created_at, "updated": updated_at,
                })
    finally:
        engine.dispose()

    command.upgrade(config, PHASE4_REVISION)
    engine = _engine(database_url)
    try:
        with engine.connect() as connection:
            current = connection.execute(text(
                "SELECT current_snapshot_id FROM projects WHERE id = :id"
            ), {"id": project_id}).scalar_one()
            rows = connection.execute(text(
                """
                SELECT id, index_artifact_path, graph_artifact_path,
                       file_count, node_count, edge_count
                FROM project_snapshots
                WHERE project_id = :project_id
                ORDER BY updated_at
                """
            ), {"project_id": project_id}).all()
        assert current == latest_id
        assert {row.id: tuple(row[1:]) for row in rows} == baseline

        invalid_id = uuid4()
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(text(
                    """
                    INSERT INTO project_snapshots
                        (id, project_id, commit_sha, status, created_at, updated_at)
                    VALUES (:id, :project_id, :sha, 'unknown', now(), now())
                    """
                ), {"id": invalid_id, "project_id": project_id, "sha": "c" * 40})
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(text(
                    """
                    INSERT INTO project_snapshots
                        (id, project_id, commit_sha, status, created_at, updated_at)
                    VALUES (:id, :project_id, :sha, 'ready', now(), now())
                    """
                ), {"id": uuid4(), "project_id": project_id, "sha": "d" * 40})
        with engine.begin() as connection:
            indexing_id = uuid4()
            connection.execute(text(
                """
                INSERT INTO project_snapshots
                    (id, project_id, commit_sha, status, error_message,
                     created_at, updated_at)
                VALUES (:id, :project_id, :sha, 'indexing', NULL, now(), now())
                """
            ), {"id": indexing_id, "project_id": project_id, "sha": "e" * 40})
            connection.execute(
                text("DELETE FROM project_snapshots WHERE id = :id"), {"id": indexing_id},
            )
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(text(
                    "UPDATE projects SET current_snapshot_id = :snapshot WHERE id = :project"
                ), {"snapshot": uuid4(), "project": project_id})
    finally:
        engine.dispose()

    command.downgrade(config, PHASE3_REVISION)
    phase3_engine = _engine(database_url)
    try:
        project_columns = {column["name"] for column in inspect(phase3_engine).get_columns("projects")}
        snapshot_columns = {
            column["name"]: column for column in inspect(phase3_engine).get_columns("project_snapshots")
        }
        assert "current_snapshot_id" not in project_columns
        assert "error_message" not in snapshot_columns
        assert all(not snapshot_columns[name]["nullable"] for name in (
            "file_count", "node_count", "edge_count",
        ))
        with phase3_engine.connect() as connection:
            rows = connection.execute(text(
                """
                SELECT id, index_artifact_path, graph_artifact_path,
                       file_count, node_count, edge_count
                FROM project_snapshots
                WHERE project_id = :project_id
                """
            ), {"project_id": project_id}).all()
        assert {row.id: tuple(row[1:]) for row in rows} == baseline
    finally:
        phase3_engine.dispose()

    command.upgrade(config, PHASE4_REVISION)
    final_engine = _engine(database_url)
    try:
        with final_engine.connect() as connection:
            assert connection.execute(text(
                "SELECT current_snapshot_id FROM projects WHERE id = :id"
            ), {"id": project_id}).scalar_one() == latest_id
    finally:
        final_engine.dispose()


def test_repository_rejects_current_snapshot_from_another_project(phase3_database):
    database_url, config = phase3_database
    command.upgrade(config, PHASE4_REVISION)
    engine = _engine(database_url)
    session = Session(engine, autoflush=False, expire_on_commit=False)
    uow = SqlAlchemyUnitOfWork(session)
    try:
        with uow.transaction():
            first = uow.projects.create("First", "https://github.com/example/first.git")
            second = uow.projects.create("Second", "https://github.com/example/second.git")
            first_snapshot = uow.snapshots.upsert(
                first.id, "1" * 40,
                index_artifact_path="artifacts/first/index.json",
                graph_artifact_path="artifacts/first/code_graph.json",
                summary=IndexSummary(1, 2, 1),
            )
            second_snapshot = uow.snapshots.upsert(
                second.id, "2" * 40,
                index_artifact_path="artifacts/second/index.json",
                graph_artifact_path="artifacts/second/code_graph.json",
                summary=IndexSummary(2, 3, 2),
            )
            uow.projects.set_current_snapshot(first.id, first_snapshot.id)

        with pytest.raises(BackendError) as failure:
            with uow.transaction():
                uow.projects.set_current_snapshot(first.id, second_snapshot.id)
        assert failure.value.code == "database_conflict"

        with uow.transaction():
            persisted = uow.projects.get(first.id)
        assert persisted.current_snapshot_id == first_snapshot.id
    finally:
        session.close()
        engine.dispose()
