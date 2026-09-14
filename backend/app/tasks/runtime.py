from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator

from backend.app.db.session import DatabaseSessionManager
from backend.app.infrastructure.artifacts import SnapshotArtifactStore
from backend.app.infrastructure.agent_workspace import AgentSnapshotWorkspace
from backend.app.infrastructure.projects import ProjectOperationLocks
from backend.app.integrations.pycode import PyCodeAdapter
from backend.app.repositories import SqlAlchemyUnitOfWork
from backend.app.runtime import (
    build_agent_run_service,
    build_project_service,
    repository_workspace_from_environment,
)
from backend.app.services.agent_runs import AgentRunService
from backend.app.services.background_tasks import BackgroundTaskService
from backend.app.services.projects import ProjectService


_database = DatabaseSessionManager()
_workspace = repository_workspace_from_environment()
_artifacts = SnapshotArtifactStore(_workspace.root)
_adapter = PyCodeAdapter()
_project_operations = ProjectOperationLocks()
_agent_workspace = AgentSnapshotWorkspace()


@contextmanager
def worker_background_task_service() -> Iterator[BackgroundTaskService]:
    session = _database.open_session()
    try:
        yield BackgroundTaskService(SqlAlchemyUnitOfWork(session))
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
            BackgroundTaskService(unit_of_work),
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
        background_tasks = BackgroundTaskService(unit_of_work)
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
        )
    finally:
        if session.in_transaction():
            session.rollback()
        session.close()
