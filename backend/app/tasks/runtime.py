from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator

from backend.app.db.session import DatabaseSessionManager
from backend.app.config import (
    ExecutionLeaseSettings, QueuedReconciliationSettings,
    StaleReconciliationSettings,
)
from backend.app.infrastructure.celery import CeleryTaskDispatcher
from backend.app.infrastructure.artifacts import SnapshotArtifactStore
from backend.app.infrastructure.agent_workspace import AgentSnapshotWorkspace
from backend.app.infrastructure.execution import ExecutionOwnership
from backend.app.infrastructure.realtime_trace import RedisStreamPublisher
from backend.app.integrations.pycode import PyCodeAdapter
from backend.app.integrations.llm import backend_llm_client_factory_from_environment
from backend.app.repositories import SqlAlchemyUnitOfWork
from backend.app.runtime import (
    build_agent_run_service,
    build_project_service,
    project_operation_locks_from_environment,
    repository_workspace_from_environment,
)
from backend.app.services.agent_runs import AgentRunService
from backend.app.services.background_tasks import BackgroundTaskService
from backend.app.services.projects import ProjectService
from backend.app.services.queued_reconciliation import QueuedTaskReconciler
from backend.app.services.stale_reconciliation import StaleRunningReconciler


_database = DatabaseSessionManager()
_workspace = repository_workspace_from_environment()
_artifacts = SnapshotArtifactStore(_workspace.root)
_adapter = PyCodeAdapter(
    llm_client_factory=backend_llm_client_factory_from_environment(),
)
_project_operations = project_operation_locks_from_environment()
_agent_workspace = AgentSnapshotWorkspace()
_reconciliation_settings = QueuedReconciliationSettings.from_environment()
_lease_settings = ExecutionLeaseSettings.from_environment()
_stale_settings = StaleReconciliationSettings.from_environment()
_dispatcher = CeleryTaskDispatcher()
_trace_publisher = RedisStreamPublisher()

if _lease_settings.ttl_seconds <= (
    _project_operations.ttl_ms / 1000
    + max(
        _lease_settings.heartbeat_interval_seconds,
        _stale_settings.interval_seconds,
    )
):
    raise RuntimeError(
        "PYCODE_TASK_LEASE_TTL_SECONDS must exceed the Redis project lock TTL "
        "by at least one heartbeat/scanner scheduling interval.",
    )


def _background_task_service(unit_of_work) -> BackgroundTaskService:
    return BackgroundTaskService(
        unit_of_work,
        lease_settings=_lease_settings,
        heartbeat_factory=lambda task: ExecutionOwnership(
            _database, task, _lease_settings,
        ),
    )


@contextmanager
def worker_background_task_service() -> Iterator[BackgroundTaskService]:
    session = _database.open_session()
    try:
        yield _background_task_service(SqlAlchemyUnitOfWork(session))
    finally:
        if session.in_transaction():
            session.rollback()
        session.close()


@contextmanager
def worker_repository_index_services(
) -> Iterator[tuple[BackgroundTaskService, ProjectService]]:
    session = _database.open_session()
    unit_of_work = SqlAlchemyUnitOfWork(session)
    try:
        yield (
            _background_task_service(unit_of_work),
            build_project_service(
                unit_of_work, adapter=_adapter, workspace=_workspace,
                artifacts=_artifacts, operations=_project_operations,
            ),
        )
    finally:
        if session.in_transaction():
            session.rollback()
        session.close()


@contextmanager
def worker_agent_run_service() -> Iterator[AgentRunService]:
    session = _database.open_session()
    unit_of_work = SqlAlchemyUnitOfWork(session)
    try:
        background_tasks = _background_task_service(unit_of_work)
        projects = build_project_service(
            unit_of_work,
            adapter=_adapter,
            workspace=_workspace,
            artifacts=_artifacts,
            operations=_project_operations,
        )
        yield build_agent_run_service(
            unit_of_work,
            background_tasks=background_tasks,
            projects=projects,
            adapter=_adapter,
            agent_workspace=_agent_workspace,
            trace_publisher=_trace_publisher,
        )
    finally:
        if session.in_transaction():
            session.rollback()
        session.close()


@contextmanager
def worker_queued_task_reconciler() -> Iterator[QueuedTaskReconciler]:
    session = _database.open_session()
    try:
        yield QueuedTaskReconciler(
            SqlAlchemyUnitOfWork(session),
            _dispatcher,
            _reconciliation_settings,
        )
    finally:
        if session.in_transaction():
            session.rollback()
        session.close()


@contextmanager
def worker_stale_running_reconciler() -> Iterator[StaleRunningReconciler]:
    session = _database.open_session()
    try:
        yield StaleRunningReconciler(
            SqlAlchemyUnitOfWork(session), _project_operations, _stale_settings,
        )
    finally:
        if session.in_transaction():
            session.rollback()
        session.close()
