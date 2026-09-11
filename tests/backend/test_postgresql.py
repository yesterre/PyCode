from __future__ import annotations

import os
import shutil
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from backend.app.core.errors import BackendError
from backend.app.core.models import AgentRunStatus, IndexSummary, ProjectStatus
from backend.app.infrastructure.repositories import (
    GitRepositorySource, RepositoryRevision, RepositoryWorkspace,
)
from backend.app.main import create_app
from backend.app.repositories import SqlAlchemyUnitOfWork


MANAGED_TABLES = {"projects", "project_snapshots", "agent_runs", "trace_events"}


def _safe_test_database_url() -> str:
    value = os.environ.get("TEST_DATABASE_URL")
    if not value:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    try:
        database = (make_url(value).database or "").lower()
    except Exception:
        pytest.fail("TEST_DATABASE_URL is not a valid SQLAlchemy URL", pytrace=False)
    if database != "pycode_test":
        pytest.fail("Destructive database tests require the pycode_test database", pytrace=False)
    development = os.environ.get("DATABASE_URL")
    if development:
        try:
            same_database_url = make_url(value) == make_url(development)
        except Exception:
            same_database_url = value == development
        if same_database_url:
            pytest.fail("TEST_DATABASE_URL must not equal DATABASE_URL", pytrace=False)
    return value


@pytest.fixture(scope="session")
def postgresql_engine() -> Engine:
    database_url = _safe_test_database_url()
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_engine(database_url, poolclass=NullPool, hide_parameters=True)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def clean_postgresql(postgresql_engine: Engine):
    _safe_test_database_url()
    _truncate(postgresql_engine)
    yield
    _safe_test_database_url()
    _truncate(postgresql_engine)


def _truncate(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text(
            "TRUNCATE TABLE trace_events, agent_runs, project_snapshots, projects CASCADE"
        ))


def _uow(engine: Engine) -> tuple[Session, SqlAlchemyUnitOfWork]:
    session = Session(engine, autoflush=False, expire_on_commit=False)
    return session, SqlAlchemyUnitOfWork(session)


def _create_project(uow: SqlAlchemyUnitOfWork, name: str = "Example"):
    with uow.transaction():
        return uow.projects.create(name, "https://github.com/example/demo.git")


def test_initial_migration_creates_exact_business_tables(postgresql_engine):
    tables = set(inspect(postgresql_engine).get_table_names())
    assert MANAGED_TABLES <= tables
    assert "alembic_version" in tables


def test_project_create_update_duplicates_and_new_session_persistence(postgresql_engine):
    session, uow = _uow(postgresql_engine)
    try:
        first = _create_project(uow, "First")
        second = _create_project(uow, "Second")
        with uow.transaction():
            updated = uow.projects.set_status(
                first.id, ProjectStatus.PREPARING, workspace_path="C:/managed/repo",
            )
        assert first.id != second.id and first.repo_url == second.repo_url
        assert updated.status == ProjectStatus.PREPARING
    finally:
        session.close()

    other_session, other = _uow(postgresql_engine)
    try:
        with other.transaction():
            persisted = other.projects.get(first.id)
        assert persisted.workspace_path == "C:/managed/repo"
        assert persisted.created_at.utcoffset() is not None
        with pytest.raises(BackendError, match="Project does not exist"):
            with other.transaction():
                other.projects.get(uuid4())
    finally:
        other_session.close()


def test_snapshot_upsert_fk_unique_and_summary(postgresql_engine):
    session, uow = _uow(postgresql_engine)
    try:
        project = _create_project(uow)
        with uow.transaction():
            first = uow.snapshots.upsert(
                project.id, "a" * 40,
                index_artifact_path=".pclens/index.json",
                graph_artifact_path=".pclens/code_graph.json",
                summary=IndexSummary(2, 4, 3),
            )
        with uow.transaction():
            second = uow.snapshots.upsert(
                project.id, "a" * 40,
                index_artifact_path=".pclens/index.json",
                graph_artifact_path=".pclens/code_graph.json",
                summary=IndexSummary(3, 7, 5),
            )
            snapshots = uow.snapshots.list_for_project(project.id)
        assert first.id == second.id
        assert len(snapshots) == 1
        assert (snapshots[0].file_count, snapshots[0].node_count, snapshots[0].edge_count) == (3, 7, 5)

        with pytest.raises(BackendError) as failure:
            with uow.transaction():
                uow.snapshots.upsert(
                    uuid4(), "b" * 40,
                    index_artifact_path=None, graph_artifact_path=None,
                    summary=IndexSummary(0, 0, 0),
                )
        assert failure.value.code == "database_conflict"
    finally:
        session.close()


def test_agent_runs_and_trace_events_persist_jsonb_order_and_unique(postgresql_engine):
    session, uow = _uow(postgresql_engine)
    try:
        project = _create_project(uow)
        with uow.transaction():
            snapshot = uow.snapshots.upsert(
                project.id, "c" * 40,
                index_artifact_path=".pclens/index.json",
                graph_artifact_path=".pclens/code_graph.json",
                summary=IndexSummary(1, 2, 1),
            )
            completed = uow.agent_runs.create(
                project.id, snapshot.id, task="question", run_type="ask", model="demo",
            )
        with uow.transaction():
            completed = uow.agent_runs.complete(completed.id, "answer")
            failed = uow.agent_runs.create(
                project.id, snapshot.id, task="main.py", run_type="impact", model=None,
            )
        with uow.transaction():
            failed = uow.agent_runs.fail(failed.id, "safe failure")
            uow.trace_events.create(completed.id, 1, "tool_finished", payload={"ok": True, "count": 2})
            uow.trace_events.create(
                completed.id, 0, "tool_started", tool_name="query_graph",
                payload={"arguments": {"symbol": "main"}},
            )
        with uow.transaction():
            events = uow.trace_events.list_for_run(completed.id)
            runs = uow.agent_runs.list_for_project(project.id)
        assert completed.status == AgentRunStatus.COMPLETED and completed.result == "answer"
        assert completed.prompt_tokens is None and completed.completion_tokens is None
        assert failed.status == AgentRunStatus.FAILED and failed.error_message == "safe failure"
        assert [event.sequence for event in events] == [0, 1]
        assert events[0].payload == {"arguments": {"symbol": "main"}}
        assert len(runs) == 2

        with pytest.raises(BackendError) as duplicate:
            with uow.transaction():
                uow.trace_events.create(completed.id, 1, "duplicate")
        assert duplicate.value.code == "database_conflict"
        with uow.transaction():
            assert len(uow.trace_events.list_for_run(completed.id)) == 2
    finally:
        session.close()


def test_transaction_failure_rolls_back_all_new_records(postgresql_engine):
    session, uow = _uow(postgresql_engine)
    project_id = uuid4()
    try:
        with pytest.raises(BackendError) as failure:
            with uow.transaction():
                project = uow.projects.create("Rollback", "https://github.com/example/rollback.git")
                project_id = project.id
                uow.trace_events.create(uuid4(), 0, "invalid_foreign_key")
        assert failure.value.code == "database_conflict"
        with pytest.raises(BackendError) as missing:
            with uow.transaction():
                uow.projects.get(project_id)
        assert missing.value.code == "project_not_found"
    finally:
        session.close()


class RestartCopySource(GitRepositorySource):
    def __init__(self, source: Path) -> None:
        super().__init__()
        self.source = source

    def clone(self, repo_url, branch, destination, *, project_id=None):
        shutil.copytree(self.source, destination)
        (destination / ".git").mkdir()
        return RepositoryRevision(branch or "default", "d" * 40)

    def update(self, repo_url, branch, repository, *, project_id=None):
        return RepositoryRevision(branch or "default", "d" * 40)

    def checkout_exact(self, repository, commit_sha, *, project_id=None):
        if commit_sha != "d" * 40:
            raise AssertionError("unexpected fake Commit")

    def protect_snapshot(
        self, repository, snapshot_id, commit_sha, *, project_id=None,
    ):
        if commit_sha != "d" * 40:
            raise AssertionError("unexpected fake Commit")

    def commit_available(self, repository, commit_sha, *, project_id=None):
        return commit_sha == "d" * 40

    def resolve_head(self, repository, *, project_id=None):
        return "d" * 40


def test_app_restart_recovers_project_snapshot_workspace_and_runs(
    postgresql_engine, tmp_path, source_repository, adapter,
):
    database_url = _safe_test_database_url()
    workspace_root = tmp_path / "persistent-workspace"
    first_workspace = RepositoryWorkspace(workspace_root, RestartCopySource(source_repository))
    first_app = create_app(adapter=adapter, workspace=first_workspace, database_url=database_url)
    with TestClient(first_app) as client:
        created = client.post(
            "/api/v1/projects",
            json={"name": "Persistent", "repo_url": "https://github.com/example/demo.git"},
        )
        assert created.status_code == 201
        project_id = UUID(created.json()["id"])
        indexed = client.post(f"/api/v1/projects/{project_id}/index")
        assert indexed.status_code == 200 and indexed.json()["status"] == "ready"

    second_workspace = RepositoryWorkspace(workspace_root, RestartCopySource(source_repository))
    second_app = create_app(adapter=adapter, workspace=second_workspace, database_url=database_url)
    with TestClient(second_app) as client:
        restored = client.get(f"/api/v1/projects/{project_id}")
        assert restored.status_code == 200
        assert restored.json()["index_summary"] == indexed.json()["index_summary"]
        answer = client.post(
            f"/api/v1/projects/{project_id}/ask", json={"question": "entry"},
        )
        assert answer.status_code == 200
        repeated = client.post(f"/api/v1/projects/{project_id}/index")
        assert repeated.status_code == 200

    session, uow = _uow(postgresql_engine)
    try:
        with uow.transaction():
            snapshots = uow.snapshots.list_for_project(project_id)
            runs = uow.agent_runs.list_for_project(project_id)
        assert len(snapshots) == 1 and snapshots[0].commit_sha == "d" * 40
        assert len(runs) == 1 and runs[0].status == AgentRunStatus.COMPLETED
    finally:
        session.close()
