from __future__ import annotations

import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Lock
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select, text, update
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from backend.app.core.errors import BackendError
from backend.app.config import QueuedReconciliationSettings, StaleReconciliationSettings
from backend.app.core.models import (
    AgentCoreStartStatus, AgentRunStatus, BackgroundTaskClaimStatus,
    BackgroundTaskStatus, IndexSummary, ProjectStatus,
)
from backend.app.infrastructure.repositories import (
    GitRepositorySource, RepositoryRevision, RepositoryWorkspace,
)
from backend.app.infrastructure.projects import (
    InMemoryProjectLockBackend, ProjectOperationLocks,
)
from backend.app.infrastructure.artifacts import SnapshotArtifactBundle
from backend.app.main import create_app
from backend.app.db.models import AgentRunORM, BackgroundTaskORM
from backend.app.repositories import SqlAlchemyUnitOfWork
from backend.app.runtime import build_project_service
from backend.app.services.background_tasks import BackgroundTaskService
from backend.app.services.idempotency import agent_run_fingerprint
from backend.app.services.queued_reconciliation import QueuedTaskReconciler
from backend.app.services.stale_reconciliation import StaleRunningReconciler
from backend.app.services.projects import ProjectService


MANAGED_TABLES = {
    "projects", "project_snapshots", "agent_runs", "trace_events", "background_tasks", "users",
}


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
            "TRUNCATE TABLE background_tasks, trace_events, agent_runs, "
            "project_snapshots, projects, users CASCADE"
        ))


def _uow(engine: Engine) -> tuple[Session, SqlAlchemyUnitOfWork]:
    session = Session(engine, autoflush=False, expire_on_commit=False)
    return session, SqlAlchemyUnitOfWork(session)


def _execute_index_task(engine: Engine, app, task_id: UUID):
    session, uow = _uow(engine)
    try:
        projects = build_project_service(
            uow, adapter=app.state.adapter, workspace=app.state.workspace,
            artifacts=app.state.artifacts, operations=app.state.project_operations,
        )
        return BackgroundTaskService(uow).execute_repository_index(
            task_id, projects.index,
        )
    finally:
        if session.in_transaction():
            session.rollback()
        session.close()


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
            completed = uow.agent_runs.create_queued(
                project.id, snapshot.id, task="question", run_type="ask", model="demo",
            )
        assert completed.status == AgentRunStatus.QUEUED
        assert completed.started_at is None and completed.finished_at is None
        with uow.transaction():
            completed = uow.agent_runs.mark_running(completed.id)
        assert completed.status == AgentRunStatus.RUNNING
        assert completed.started_at is not None and completed.finished_at is None
        with uow.transaction():
            completed = uow.agent_runs.complete(completed.id, "answer")
            failed = uow.agent_runs.create(
                project.id, snapshot.id, task="main.py", run_type="impact", model=None,
            )
        with uow.transaction():
            failed = uow.agent_runs.fail(failed.id, "safe failure")
            uow.trace_events.create(
                completed.id, 1, "tool_finished", tool_name="query_graph",
                payload={
                    "schema_version": 1,
                    "occurred_at": "2026-09-16T08:00:01Z",
                    "event_payload": {"outcome": "success", "duration_ms": 2},
                },
            )
            uow.trace_events.create(
                completed.id, 0, "tool_started", tool_name="query_graph",
                payload={
                    "schema_version": 1,
                    "occurred_at": "2026-09-16T08:00:00Z",
                    "event_payload": {"arguments": {"symbol": "main"}},
                },
            )
        with uow.transaction():
            events = uow.trace_events.list_for_run(completed.id)
            runs = uow.agent_runs.list_for_project(project.id)
        assert completed.status == AgentRunStatus.COMPLETED and completed.result == "answer"
        assert completed.prompt_tokens is None and completed.completion_tokens is None
        assert failed.status == AgentRunStatus.FAILED and failed.error_message == "safe failure"
        assert [event.sequence for event in events] == [0, 1]
        assert events[0].payload["schema_version"] == 1
        assert events[0].payload["event_payload"] == {
            "arguments": {"symbol": "main"},
        }
        assert len(runs) == 2

        with pytest.raises(BackendError) as duplicate:
            with uow.transaction():
                uow.trace_events.create(completed.id, 1, "duplicate")
        assert duplicate.value.code == "database_conflict"
        with uow.transaction():
            assert len(uow.trace_events.list_for_run(completed.id)) == 2
    finally:
        session.close()


def test_agent_run_history_query_filters_orders_and_pages_in_postgresql(postgresql_engine):
    session, uow = _uow(postgresql_engine)
    try:
        project = _create_project(uow)
        other = _create_project(uow, "Other")
        with uow.transaction():
            ask = uow.agent_runs.create(
                project.id, None, task="question", run_type="ask", model=None,
            )
            ask = uow.agent_runs.complete(ask.id, "answer")
            agent = uow.agent_runs.create(
                project.id, None, task="task", run_type="agent", model=None,
            )
            agent = uow.agent_runs.complete(agent.id, "result")
            unknown = uow.agent_runs.create(
                project.id, None, task="unknown", run_type="unknown", model=None,
            )
            uow.agent_runs.complete(unknown.id, "hidden")
            empty = uow.agent_runs.create(
                project.id, None, task="empty", run_type="impact", model=None,
            )
            uow.agent_runs.complete(empty.id, None)
            uow.agent_runs.create_queued(
                project.id, None, task="queued", run_type="agent", model=None,
            )
            foreign = uow.agent_runs.create(
                other.id, None, task="foreign", run_type="ask", model=None,
            )
            uow.agent_runs.complete(foreign.id, "hidden")

        tied = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)
        with session.begin():
            session.execute(
                update(AgentRunORM)
                .where(AgentRunORM.id.in_((ask.id, agent.id)))
                .values(created_at=tied)
            )
        expected = sorted((ask.id, agent.id), reverse=True)
        with uow.transaction():
            first = uow.agent_runs.list_history_for_project(project.id, 1, 0)
            second = uow.agent_runs.list_history_for_project(project.id, 2, 1)
        assert [run.id for run in first] == expected[:1]
        assert [run.id for run in second] == expected[1:]
    finally:
        session.close()


def test_agent_core_marker_persists_and_survives_terminal_transition(postgresql_engine):
    session, uow = _uow(postgresql_engine)
    try:
        project = _create_project(uow)
        with uow.transaction():
            run = uow.agent_runs.create_queued(
                project.id, None, task="agent", run_type="agent", model=None,
            )
            assert run.core_started_at is None
            invalid = uow.agent_runs.mark_core_started(run.id)
        assert invalid.status == AgentCoreStartStatus.INVALID_STATE

        with uow.transaction():
            uow.agent_runs.mark_running(run.id)
            marked = uow.agent_runs.mark_core_started(run.id)
        assert marked.status == AgentCoreStartStatus.MARKED
        assert marked.run is not None and marked.run.core_started_at is not None
        marker = marked.run.core_started_at

        with uow.transaction():
            already = uow.agent_runs.mark_core_started(run.id)
            completed = uow.agent_runs.complete(run.id, "answer")
        assert already.status == AgentCoreStartStatus.ALREADY_MARKED
        assert completed.core_started_at == marker
    finally:
        session.close()

    with Session(postgresql_engine) as verification:
        persisted = verification.get(AgentRunORM, run.id)
        assert persisted is not None
        assert persisted.core_started_at == marker
        assert persisted.status == "completed"


def test_postgresql_concurrent_agent_core_marker_has_one_winner(postgresql_engine):
    session, uow = _uow(postgresql_engine)
    try:
        project = _create_project(uow)
        with uow.transaction():
            run = uow.agent_runs.create_queued(
                project.id, None, task="agent", run_type="agent", model=None,
            )
            uow.agent_runs.mark_running(run.id)
    finally:
        session.close()

    barrier = Barrier(2)

    def mark():
        worker_session, worker = _uow(postgresql_engine)
        try:
            barrier.wait()
            with worker.transaction():
                return worker.agent_runs.mark_core_started(run.id).status
        finally:
            worker_session.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(lambda _: mark(), range(2)))

    assert sorted(statuses) == sorted([
        AgentCoreStartStatus.MARKED,
        AgentCoreStartStatus.ALREADY_MARKED,
    ])
    with Session(postgresql_engine) as verification:
        persisted = verification.get(AgentRunORM, run.id)
        assert persisted is not None and persisted.core_started_at is not None


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


def test_background_task_lifecycle_persists_across_sessions(postgresql_engine):
    session, uow = _uow(postgresql_engine)
    try:
        with uow.transaction():
            queued = uow.background_tasks.create("health")
        with uow.transaction():
            running = uow.background_tasks.mark_running(queued.id)
        assert running.status == BackgroundTaskStatus.RUNNING
        assert running.attempt_count == 1 and running.started_at is not None
    finally:
        session.close()

    other_session, other = _uow(postgresql_engine)
    try:
        with other.transaction():
            completed = other.background_tasks.mark_completed(queued.id)
        assert completed.status == BackgroundTaskStatus.COMPLETED
        assert completed.finished_at is not None
        with other.transaction():
            failed_candidate = other.background_tasks.create("health")
            other.background_tasks.mark_running(failed_candidate.id)
        with other.transaction():
            failed = other.background_tasks.mark_failed(
                failed_candidate.id,
                error_code="health_probe_failed",
                error_message="The health probe failed safely.",
            )
        assert failed.status == BackgroundTaskStatus.FAILED
        assert failed.error_code == "health_probe_failed"
        assert failed.error_message == "The health probe failed safely."
    finally:
        other_session.close()


def test_background_task_postgresql_claim_is_atomic(postgresql_engine):
    session, uow = _uow(postgresql_engine)
    try:
        with uow.transaction():
            task = uow.background_tasks.create("health")
    finally:
        session.close()


def _expire_task(engine: Engine, task_id: UUID) -> None:
    with engine.begin() as connection:
        connection.execute(
            update(BackgroundTaskORM)
            .where(BackgroundTaskORM.id == task_id)
            .values(lease_expires_at=func.now() - timedelta(seconds=1))
        )


def _project_locks() -> ProjectOperationLocks:
    return ProjectOperationLocks(
        InMemoryProjectLockBackend(), ttl_seconds=300,
        renewal_interval_seconds=60,
    )


def _create_stale_repository_task(engine: Engine):
    session, uow = _uow(engine)
    try:
        project = _create_project(uow)
        with uow.transaction():
            snapshot = uow.snapshots.start(project.id, "d" * 40)
            task = uow.background_tasks.create(
                "repository_index", project_id=project.id,
                snapshot_id=snapshot.id,
            )
            task = uow.background_tasks.claim(task.id).task
        assert task is not None and task.execution_token is not None
    finally:
        session.close()
    _expire_task(engine, task.id)
    return project, snapshot, task


def test_postgresql_heartbeat_scanner_race_has_one_winner(postgresql_engine):
    _, _, task = _create_stale_repository_task(postgresql_engine)
    barrier = Barrier(2)

    def heartbeat():
        session, uow = _uow(postgresql_engine)
        try:
            barrier.wait(timeout=5)
            with uow.transaction():
                return uow.background_tasks.heartbeat(
                    task.id, task.execution_token, lease_ttl_seconds=420,
                ) is not None
        finally:
            session.close()

    def recover():
        session, uow = _uow(postgresql_engine)
        try:
            barrier.wait(timeout=5)
            with uow.transaction():
                return uow.background_tasks.requeue_if_stale(
                    task.id, task.execution_token,
                ) is not None
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        heartbeat_won, scanner_won = list(pool.map(lambda fn: fn(), [heartbeat, recover]))
    assert int(heartbeat_won) + int(scanner_won) == 1
    with Session(postgresql_engine) as verification:
        persisted = verification.get(BackgroundTaskORM, task.id)
        assert persisted is not None
        if heartbeat_won:
            assert persisted.status == "running" and persisted.execution_token == task.execution_token
        else:
            assert persisted.status == "queued" and persisted.execution_token is None


def test_postgresql_concurrent_stale_scanners_requeue_once(postgresql_engine):
    _, _, task = _create_stale_repository_task(postgresql_engine)
    barrier = Barrier(2)

    def scan():
        session, uow = _uow(postgresql_engine)
        try:
            barrier.wait(timeout=5)
            return StaleRunningReconciler(
                uow, _project_locks(), StaleReconciliationSettings(),
            ).reconcile()
        finally:
            if session.in_transaction():
                session.rollback()
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: scan(), range(2)))
    assert sum(result.requeued for result in results) == 1
    with Session(postgresql_engine) as verification:
        persisted = verification.get(BackgroundTaskORM, task.id)
        assert persisted is not None and persisted.status == "queued"
        assert persisted.execution_token is None


def test_postgresql_terminal_scanner_race_has_one_transition(postgresql_engine):
    session, uow = _uow(postgresql_engine)
    try:
        with uow.transaction():
            task = uow.background_tasks.create("health")
            task = uow.background_tasks.claim(task.id).task
        assert task is not None and task.execution_token is not None
    finally:
        session.close()
    _expire_task(postgresql_engine, task.id)
    barrier = Barrier(2)

    def terminal():
        session, uow = _uow(postgresql_engine)
        try:
            barrier.wait(timeout=5)
            with uow.transaction():
                return uow.background_tasks.mark_completed(
                    task.id, execution_token=task.execution_token,
                ) is not None
        finally:
            session.close()

    def scanner():
        session, uow = _uow(postgresql_engine)
        try:
            barrier.wait(timeout=5)
            with uow.transaction():
                return uow.background_tasks.fail_if_stale(
                    task.id, task.execution_token,
                    error_code="worker_execution_lost", error_message="lost",
                ) is not None
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda fn: fn(), [terminal, scanner]))
    assert sum(outcomes) == 1
    with Session(postgresql_engine) as verification:
        persisted = verification.get(BackgroundTaskORM, task.id)
        assert persisted is not None and persisted.status in {"completed", "failed"}


def test_postgresql_agent_marker_scanner_race_is_not_split_brain(postgresql_engine):
    session, uow = _uow(postgresql_engine)
    try:
        project = _create_project(uow)
        with uow.transaction():
            snapshot = uow.snapshots.start(project.id, "e" * 40)
            snapshot = uow.snapshots.mark_ready(
                snapshot.id, index_artifact_path="index.json",
                graph_artifact_path="graph.json", summary=IndexSummary(1, 1, 0),
            )
            uow.projects.set_current_snapshot(project.id, snapshot.id)
            uow.projects.set_status(project.id, ProjectStatus.READY)
            run = uow.agent_runs.create_queued(
                project.id, snapshot.id, task="analyze", run_type="agent", model=None,
            )
            task = uow.background_tasks.create(
                "agent_run", project_id=project.id, snapshot_id=snapshot.id,
                agent_run_id=run.id,
            )
            task = uow.background_tasks.claim(task.id).task
            uow.agent_runs.mark_running(run.id)
        assert task is not None and task.execution_token is not None
    finally:
        session.close()
    _expire_task(postgresql_engine, task.id)
    barrier = Barrier(2)

    def mark_core():
        session, uow = _uow(postgresql_engine)
        try:
            barrier.wait(timeout=5)
            with uow.transaction():
                if uow.background_tasks.guard_execution(
                    task.id, task.execution_token,
                ) is None:
                    return "lost"
                return uow.agent_runs.mark_core_started(run.id).status.value
        finally:
            session.close()

    def scan():
        session, uow = _uow(postgresql_engine)
        try:
            barrier.wait(timeout=5)
            return StaleRunningReconciler(
                uow, _project_locks(), StaleReconciliationSettings(),
            ).reconcile()
        finally:
            if session.in_transaction():
                session.rollback()
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        marker, _ = list(pool.map(lambda fn: fn(), [mark_core, scan]))
    with Session(postgresql_engine) as verification:
        persisted_task = verification.get(BackgroundTaskORM, task.id)
        persisted_run = verification.get(AgentRunORM, run.id)
        assert persisted_task is not None and persisted_run is not None
        states = (persisted_task.status, persisted_run.status)
        assert states in {("queued", "queued"), ("failed", "failed")}
        if states == ("queued", "queued"):
            assert marker == "lost" and persisted_run.core_started_at is None
        else:
            assert persisted_task.error_code == "worker_lost_after_agent_core"
            assert persisted_run.core_started_at is not None


def test_postgresql_snapshot_target_binding_is_owner_guarded_and_atomic(
    postgresql_engine,
):
    session, uow = _uow(postgresql_engine)
    try:
        project = _create_project(uow)
        with uow.transaction():
            first = uow.snapshots.start(project.id, "1" * 40)
            second = uow.snapshots.start(project.id, "2" * 40)
            task = uow.background_tasks.create(
                "repository_index", project_id=project.id,
            )
            task = uow.background_tasks.claim(task.id).task
        assert task is not None and task.execution_token is not None
        with uow.transaction():
            assert uow.background_tasks.bind_snapshot_if_owner(
                task.id, uuid4(), first.id,
            ) is None
    finally:
        session.close()

    barrier = Barrier(2)

    def bind(snapshot_id):
        worker_session, worker = _uow(postgresql_engine)
        try:
            barrier.wait(timeout=5)
            with worker.transaction():
                return worker.background_tasks.bind_snapshot_if_owner(
                    task.id, task.execution_token, snapshot_id,
                )
        finally:
            worker_session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        bound = list(pool.map(bind, [first.id, second.id]))
    assert sum(value is not None for value in bound) == 1
    with Session(postgresql_engine) as verification:
        persisted = verification.get(BackgroundTaskORM, task.id)
        assert persisted is not None
        assert persisted.snapshot_id in {first.id, second.id}


def test_postgresql_agent_pre_core_requeue_updates_both_rows(postgresql_engine):
    session, uow = _uow(postgresql_engine)
    try:
        project = _create_project(uow)
        with uow.transaction():
            snapshot = uow.snapshots.start(project.id, "3" * 40)
            snapshot = uow.snapshots.mark_ready(
                snapshot.id, index_artifact_path="index.json",
                graph_artifact_path="graph.json", summary=IndexSummary(1, 1, 0),
            )
            uow.projects.set_current_snapshot(project.id, snapshot.id)
            uow.projects.set_status(project.id, ProjectStatus.READY)
            run = uow.agent_runs.create_queued(
                project.id, snapshot.id, task="analyze", run_type="agent", model=None,
            )
            task = uow.background_tasks.create(
                "agent_run", project_id=project.id, snapshot_id=snapshot.id,
                agent_run_id=run.id,
            )
            task = uow.background_tasks.claim(task.id).task
            uow.agent_runs.mark_running(run.id)
        assert task is not None
    finally:
        session.close()
    _expire_task(postgresql_engine, task.id)

    worker_session, worker = _uow(postgresql_engine)
    try:
        result = StaleRunningReconciler(
            worker, _project_locks(), StaleReconciliationSettings(),
        ).reconcile()
        assert result.requeued == 1
    finally:
        worker_session.close()
    with Session(postgresql_engine) as verification:
        persisted_task = verification.get(BackgroundTaskORM, task.id)
        persisted_run = verification.get(AgentRunORM, run.id)
        assert persisted_task is not None and persisted_run is not None
        assert persisted_task.status == "queued"
        assert persisted_run.status == "queued"
        assert persisted_task.enqueue_confirmed_at is None
        assert persisted_task.execution_token is None


def test_postgresql_repository_activation_rejects_stale_owner(postgresql_engine):
    session, uow = _uow(postgresql_engine)
    try:
        project = _create_project(uow)
        with uow.transaction():
            snapshot = uow.snapshots.start(project.id, "4" * 40)
            task = uow.background_tasks.create(
                "repository_index", project_id=project.id,
                snapshot_id=snapshot.id,
            )
            task = uow.background_tasks.claim(task.id).task
        assert task is not None
        service = ProjectService(
            uow, adapter=object(), workspace=object(),
            operations=_project_locks(), artifacts=object(),
        )
        bundle = SnapshotArtifactBundle(
            Path("index.json"), Path("graph.json"),
            "index.json", "graph.json", IndexSummary(1, 1, 0),
        )
        with pytest.raises(BackendError) as failure:
            service._activate(
                project.id, snapshot, bundle, finalize=True,
                task_id=task.id, execution_token=uuid4(),
            )
        assert failure.value.code == "worker_execution_lost"
    finally:
        session.close()
    with Session(postgresql_engine) as verification:
        persisted_task = verification.get(BackgroundTaskORM, task.id)
        assert persisted_task is not None and persisted_task.status == "running"
        persisted_project = verification.execute(
            text("SELECT status, current_snapshot_id FROM projects WHERE id=:id"),
            {"id": project.id},
        ).one()
        assert persisted_project.status == "created"
        assert persisted_project.current_snapshot_id is None


def test_postgresql_concurrent_queued_scanners_reserve_and_publish_once(
    postgresql_engine,
):
    now = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)
    session, uow = _uow(postgresql_engine)
    try:
        project = _create_project(uow)
        with uow.transaction():
            task = uow.background_tasks.create(
                "repository_index",
                project_id=project.id,
                next_enqueue_attempt_at=now,
            )
    finally:
        session.close()

    class Dispatcher:
        def __init__(self):
            self.task_ids: list[UUID] = []
            self.guard = Lock()

        def enqueue_health(self, task_id: UUID) -> None:
            raise AssertionError("unexpected task type")

        def enqueue_repository_index(self, task_id: UUID) -> None:
            with self.guard:
                self.task_ids.append(task_id)

        def enqueue_agent_run(self, task_id: UUID) -> None:
            raise AssertionError("unexpected task type")

    dispatcher = Dispatcher()
    settings = QueuedReconciliationSettings(0, 1, 10, 60)
    barrier = Barrier(2)

    def reconcile():
        worker_session, worker_uow = _uow(postgresql_engine)
        try:
            barrier.wait()
            return QueuedTaskReconciler(
                worker_uow, dispatcher, settings, clock=lambda: now,
            ).run()
        finally:
            if worker_session.in_transaction():
                worker_session.rollback()
            worker_session.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: reconcile(), range(2)))

    assert sum(result.candidate_count for result in results) == 1
    assert dispatcher.task_ids == [task.id]
    with Session(postgresql_engine) as verification:
        persisted = verification.get(BackgroundTaskORM, task.id)
        assert persisted is not None
        assert persisted.status == "queued"
        assert persisted.enqueue_attempt_count == 1
        assert persisted.enqueue_confirmed_at == now
        assert persisted.next_enqueue_attempt_at is None
        assert persisted.attempt_count == 0
        assert verification.scalar(
            select(func.count()).select_from(BackgroundTaskORM)
        ) == 1


def test_postgresql_agent_orphan_reconciliation_preserves_run_task_and_snapshot(
    postgresql_engine,
):
    now = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)
    session, uow = _uow(postgresql_engine)
    try:
        project = _create_project(uow)
        with uow.transaction():
            snapshot = uow.snapshots.upsert(
                project.id, "a" * 40,
                index_artifact_path="index.json", graph_artifact_path="graph.json",
                summary=IndexSummary(1, 2, 1),
            )
            run = uow.agent_runs.create_queued(
                project.id, snapshot.id,
                task="Analyze", run_type="agent", model=None,
            )
            task = uow.background_tasks.create(
                "agent_run",
                project_id=project.id,
                snapshot_id=snapshot.id,
                agent_run_id=run.id,
                next_enqueue_attempt_at=now,
            )
    finally:
        session.close()

    class Dispatcher:
        def __init__(self):
            self.task_ids: list[UUID] = []

        def enqueue_health(self, task_id: UUID) -> None:
            raise AssertionError("unexpected task type")

        def enqueue_repository_index(self, task_id: UUID) -> None:
            raise AssertionError("unexpected task type")

        def enqueue_agent_run(self, task_id: UUID) -> None:
            self.task_ids.append(task_id)

    dispatcher = Dispatcher()
    worker_session, worker_uow = _uow(postgresql_engine)
    try:
        result = QueuedTaskReconciler(
            worker_uow,
            dispatcher,
            QueuedReconciliationSettings(0, 1, 10, 60),
            clock=lambda: now,
        ).run()
    finally:
        if worker_session.in_transaction():
            worker_session.rollback()
        worker_session.close()

    assert result.confirmed_count == 1
    assert dispatcher.task_ids == [task.id]
    with Session(postgresql_engine) as verification:
        assert verification.scalar(select(func.count()).select_from(AgentRunORM)) == 1
        assert verification.scalar(
            select(func.count()).select_from(BackgroundTaskORM)
        ) == 1
        persisted_run = verification.get(AgentRunORM, run.id)
        persisted_task = verification.get(BackgroundTaskORM, task.id)
        assert persisted_run is not None and persisted_task is not None
        assert persisted_run.snapshot_id == snapshot.id
        assert persisted_task.agent_run_id == run.id
        assert persisted_task.snapshot_id == snapshot.id

    barrier = Barrier(2)

    def claim():
        worker_session, worker_uow = _uow(postgresql_engine)
        try:
            barrier.wait(timeout=5)
            with worker_uow.transaction():
                return worker_uow.background_tasks.claim(task.id).status
        finally:
            worker_session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))

    assert sorted(results) == [
        BackgroundTaskClaimStatus.ALREADY_RUNNING,
        BackgroundTaskClaimStatus.CLAIMED,
    ]
    session, uow = _uow(postgresql_engine)
    try:
        with uow.transaction():
            persisted = uow.background_tasks.get(task.id)
        assert persisted.status == BackgroundTaskStatus.RUNNING
        assert persisted.attempt_count == 1
    finally:
        session.close()


def test_background_task_foreign_keys_are_checked(postgresql_engine):
    session, uow = _uow(postgresql_engine)
    try:
        with pytest.raises(BackendError) as failure:
            with uow.transaction():
                uow.background_tasks.create("health", project_id=uuid4())
        assert failure.value.code == "database_conflict"
    finally:
        session.close()


def _ready_postgresql_project(postgresql_engine):
    session, uow = _uow(postgresql_engine)
    try:
        project = _create_project(uow, "Idempotent")
        with uow.transaction():
            snapshot = uow.snapshots.upsert(
                project.id,
                "e" * 40,
                index_artifact_path="artifacts/e/index.json",
                graph_artifact_path="artifacts/e/code_graph.json",
                summary=IndexSummary(1, 1, 0),
            )
            uow.projects.set_current_snapshot(project.id, snapshot.id)
        return project, snapshot
    finally:
        session.close()


def _concurrent_post(apps, path: str, *, headers, json=None, client_factory=TestClient):
    barrier = Barrier(len(apps))

    def submit(app):
        with client_factory(app) as client:
            barrier.wait(timeout=10)
            payload = json(app) if callable(json) else json
            response = client.post(path, headers=headers(app), json=payload)
            return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=len(apps)) as pool:
        return list(pool.map(submit, apps))


def _postgresql_submission_apps(
    postgresql_engine, application, task_dispatcher_factory,
):
    database_url = _safe_test_database_url()
    dispatcher = task_dispatcher_factory()
    apps = [
        create_app(
            adapter=application.state.adapter,
            workspace=application.state.workspace,
            database_url=database_url,
            task_dispatcher=dispatcher,
        )
        for _ in range(2)
    ]
    return apps, dispatcher


def test_postgresql_concurrent_index_same_key_returns_one_task(
    postgresql_engine, application, task_dispatcher_factory, authenticated_client_factory,
):
    session, uow = _uow(postgresql_engine)
    try:
        project = _create_project(uow, "Same index key")
    finally:
        session.close()
    apps, dispatcher = _postgresql_submission_apps(
        postgresql_engine, application, task_dispatcher_factory,
    )
    with authenticated_client_factory(apps[0]) as client:
        owner_id = UUID(client.get("/api/v1/auth/me").json()["id"])
    with Session(postgresql_engine) as session:
        uow = SqlAlchemyUnitOfWork(session)
        with uow.transaction():
            uow.projects.assign_legacy_owner(project.id, owner_id)

    results = _concurrent_post(
        apps,
        f"/api/v1/projects/{project.id}/index",
        headers=lambda _: {"Idempotency-Key": "postgres-index-same"},
        client_factory=authenticated_client_factory,
    )

    assert [status for status, _ in results] == [202, 202]
    assert len({body["task_id"] for _, body in results}) == 1
    assert len(dispatcher.repository_index_task_ids) == 1
    with Session(postgresql_engine) as verification:
        count = verification.scalar(
            select(func.count()).select_from(BackgroundTaskORM).where(
                BackgroundTaskORM.task_type == "repository_index",
                BackgroundTaskORM.project_id == project.id,
            )
        )
    assert count == 1


def test_postgresql_concurrent_index_different_keys_has_one_active_task(
    postgresql_engine, application, task_dispatcher_factory, authenticated_client_factory,
):
    session, uow = _uow(postgresql_engine)
    try:
        project = _create_project(uow, "Active index")
    finally:
        session.close()
    apps, dispatcher = _postgresql_submission_apps(
        postgresql_engine, application, task_dispatcher_factory,
    )
    with authenticated_client_factory(apps[0]) as client:
        owner_id = UUID(client.get("/api/v1/auth/me").json()["id"])
    with Session(postgresql_engine) as session:
        uow = SqlAlchemyUnitOfWork(session)
        with uow.transaction():
            uow.projects.assign_legacy_owner(project.id, owner_id)
    keys = iter(("postgres-index-a", "postgres-index-b"))

    results = _concurrent_post(
        apps,
        f"/api/v1/projects/{project.id}/index",
        headers=lambda _: {"Idempotency-Key": next(keys)},
        client_factory=authenticated_client_factory,
    )

    assert sorted(status for status, _ in results) == [202, 409]
    conflict = next(body for status, body in results if status == 409)
    assert conflict["detail"]["code"] == "repository_index_in_progress"
    assert len(dispatcher.repository_index_task_ids) == 1
    with Session(postgresql_engine) as verification:
        active_count = verification.scalar(
            select(func.count()).select_from(BackgroundTaskORM).where(
                BackgroundTaskORM.task_type == "repository_index",
                BackgroundTaskORM.project_id == project.id,
                BackgroundTaskORM.status.in_(("queued", "running")),
            )
        )
    assert active_count == 1


def test_postgresql_concurrent_agent_same_key_has_one_run_and_task(
    postgresql_engine, application, task_dispatcher_factory,
):
    project, snapshot = _ready_postgresql_project(postgresql_engine)
    apps, dispatcher = _postgresql_submission_apps(
        postgresql_engine, application, task_dispatcher_factory,
    )
    payload = {"project_id": str(project.id), "task": "分析并发提交"}

    results = _concurrent_post(
        apps,
        "/api/v1/agent-runs",
        headers=lambda _: {"Idempotency-Key": "postgres-agent-same"},
        json=payload,
    )

    assert [status for status, _ in results] == [202, 202]
    assert len({body["run_id"] for _, body in results}) == 1
    assert len({body["task_id"] for _, body in results}) == 1
    assert len(dispatcher.agent_run_task_ids) == 1
    with Session(postgresql_engine) as verification:
        run_count = verification.scalar(
            select(func.count()).select_from(AgentRunORM).where(
                AgentRunORM.project_id == project.id,
                AgentRunORM.snapshot_id == snapshot.id,
            )
        )
        task_count = verification.scalar(
            select(func.count()).select_from(BackgroundTaskORM).where(
                BackgroundTaskORM.task_type == "agent_run",
                BackgroundTaskORM.project_id == project.id,
                BackgroundTaskORM.idempotency_key == "postgres-agent-same",
            )
        )
        persisted_task = verification.scalar(
            select(BackgroundTaskORM).where(
                BackgroundTaskORM.task_type == "agent_run",
                BackgroundTaskORM.project_id == project.id,
                BackgroundTaskORM.idempotency_key == "postgres-agent-same",
            )
        )
    assert run_count == task_count == 1
    assert persisted_task is not None
    assert str(persisted_task.agent_run_id) == results[0][1]["run_id"]


def test_postgresql_agent_same_key_different_task_conflicts(
    postgresql_engine, application, task_dispatcher_factory,
):
    project, _ = _ready_postgresql_project(postgresql_engine)
    apps, dispatcher = _postgresql_submission_apps(
        postgresql_engine, application, task_dispatcher_factory,
    )
    headers = {"Idempotency-Key": "postgres-agent-conflict"}
    with TestClient(apps[0]) as client:
        first = client.post(
            "/api/v1/agent-runs",
            headers=headers,
            json={"project_id": str(project.id), "task": "分析主要调用链"},
        )
        conflict = client.post(
            "/api/v1/agent-runs",
            headers=headers,
            json={"project_id": str(project.id), "task": "分析数据库模块"},
        )

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"
    assert len(dispatcher.agent_run_task_ids) == 1
    with Session(postgresql_engine) as verification:
        runs = verification.scalars(
            select(AgentRunORM).where(AgentRunORM.project_id == project.id)
        ).all()
        tasks = verification.scalars(
            select(BackgroundTaskORM).where(
                BackgroundTaskORM.task_type == "agent_run",
                BackgroundTaskORM.project_id == project.id,
            )
        ).all()
    assert len(runs) == len(tasks) == 1
    assert runs[0].task == "分析主要调用链"
    assert tasks[0].request_fingerprint == agent_run_fingerprint(
        project.id, "分析主要调用链",
    )


def test_postgresql_concurrent_agent_same_key_different_tasks_conflict(
    postgresql_engine, application, task_dispatcher_factory,
):
    project, _ = _ready_postgresql_project(postgresql_engine)
    apps, dispatcher = _postgresql_submission_apps(
        postgresql_engine, application, task_dispatcher_factory,
    )
    submitted_tasks = iter(("分析主要调用链", "分析数据库模块"))

    results = _concurrent_post(
        apps,
        "/api/v1/agent-runs",
        headers=lambda _: {"Idempotency-Key": "postgres-agent-race-conflict"},
        json=lambda _: {
            "project_id": str(project.id),
            "task": next(submitted_tasks),
        },
    )

    assert sorted(status for status, _ in results) == [202, 409]
    conflict = next(body for status, body in results if status == 409)
    assert conflict["detail"]["code"] == "idempotency_conflict"
    assert len(dispatcher.agent_run_task_ids) == 1
    with Session(postgresql_engine) as verification:
        runs = verification.scalars(
            select(AgentRunORM).where(AgentRunORM.project_id == project.id)
        ).all()
        tasks = verification.scalars(
            select(BackgroundTaskORM).where(
                BackgroundTaskORM.task_type == "agent_run",
                BackgroundTaskORM.project_id == project.id,
            )
        ).all()
    assert len(runs) == len(tasks) == 1
    assert runs[0].task in {"分析主要调用链", "分析数据库模块"}
    assert tasks[0].request_fingerprint == agent_run_fingerprint(
        project.id, runs[0].task,
    )


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
    authenticated_client_factory,
    postgresql_engine, tmp_path, source_repository, adapter,
    task_dispatcher_factory,
):
    database_url = _safe_test_database_url()
    workspace_root = tmp_path / "persistent-workspace"
    first_workspace = RepositoryWorkspace(workspace_root, RestartCopySource(source_repository))
    first_app = create_app(
        adapter=adapter, workspace=first_workspace, database_url=database_url,
        task_dispatcher=task_dispatcher_factory(),
    )
    with authenticated_client_factory(first_app) as client:
        created = client.post(
            "/api/v1/projects",
            json={"name": "Persistent", "repo_url": "https://github.com/example/demo.git"},
        )
        assert created.status_code == 201
        project_id = UUID(created.json()["id"])
        indexed = client.post(f"/api/v1/projects/{project_id}/index")
        assert indexed.status_code == 202 and indexed.json()["status"] == "queued"
        _execute_index_task(
            postgresql_engine, first_app, UUID(indexed.json()["task_id"]),
        )
        indexed_project = client.get(f"/api/v1/projects/{project_id}")
        assert indexed_project.status_code == 200
        assert indexed_project.json()["status"] == "ready"

    second_workspace = RepositoryWorkspace(workspace_root, RestartCopySource(source_repository))
    second_app = create_app(
        adapter=adapter, workspace=second_workspace, database_url=database_url,
        task_dispatcher=task_dispatcher_factory(),
    )
    with authenticated_client_factory(second_app) as client:
        restored = client.get(f"/api/v1/projects/{project_id}")
        assert restored.status_code == 200
        assert restored.json()["index_summary"] == indexed_project.json()["index_summary"]
        answer = client.post(
            f"/api/v1/projects/{project_id}/ask", json={"question": "entry"},
        )
        assert answer.status_code == 200
        repeated = client.post(f"/api/v1/projects/{project_id}/index")
        assert repeated.status_code == 202
        _execute_index_task(
            postgresql_engine, second_app, UUID(repeated.json()["task_id"]),
        )

    session, uow = _uow(postgresql_engine)
    try:
        with uow.transaction():
            snapshots = uow.snapshots.list_for_project(project_id)
            runs = uow.agent_runs.list_for_project(project_id)
        assert len(snapshots) == 1 and snapshots[0].commit_sha == "d" * 40
        assert len(runs) == 1 and runs[0].status == AgentRunStatus.COMPLETED
    finally:
        session.close()
