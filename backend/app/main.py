"""Run with: python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.app.config import AuthSettings, QueuedReconciliationSettings, load_environment
from backend.app.db.session import DatabaseSessionManager
from backend.app.api.errors import register_error_handlers
from backend.app.api.agent_runs import router as agent_runs_router
from backend.app.api.auth import router as auth_router
from backend.app.api.projects import router as projects_router
from backend.app.api.tasks import router as tasks_router
from backend.app.infrastructure.celery import CeleryTaskDispatcher
from backend.app.infrastructure.agent_workspace import AgentSnapshotWorkspace
from backend.app.infrastructure.artifacts import SnapshotArtifactStore
from backend.app.infrastructure.projects import ProjectOperationLocks
from backend.app.infrastructure.repositories import RepositoryWorkspace
from backend.app.integrations.pycode import PyCodeAdapter
from backend.app.integrations.llm import backend_llm_client_factory_from_environment
from backend.app.repositories import InMemoryPersistence
from backend.app.runtime import (
    project_operation_locks_from_environment,
    repository_workspace_from_environment,
)
from backend.app.services.background_tasks import BackgroundTaskDispatcher


def create_app(*, adapter: PyCodeAdapter | None = None,
               workspace: RepositoryWorkspace | None = None,
               artifacts: SnapshotArtifactStore | None = None,
               persistence: InMemoryPersistence | None = None,
               database_url: str | None = None,
               task_dispatcher: BackgroundTaskDispatcher | None = None,
               project_operations: ProjectOperationLocks | None = None,
               reconciliation_settings: QueuedReconciliationSettings | None = None,
               auth_settings: AuthSettings | None = None) -> FastAPI:
    """Use PostgreSQL by default; tests may explicitly inject memory persistence."""
    load_environment()
    database = DatabaseSessionManager(database_url) if persistence is None else None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        if app.state.realtime_redis is not None:
            await app.state.realtime_redis.aclose()
        if database is not None:
            database.dispose()

    app = FastAPI(title="PyCode Backend", version="2.0-phase5d2", lifespan=lifespan)
    if workspace is None:
        workspace = repository_workspace_from_environment()
    app.state.workspace = workspace
    app.state.artifacts = (
        artifacts if artifacts is not None else SnapshotArtifactStore(workspace.root)
    )
    app.state.adapter = (
        adapter if adapter is not None
        else PyCodeAdapter(
            llm_client_factory=backend_llm_client_factory_from_environment(),
        )
    )
    app.state.persistence = persistence
    app.state.database = database
    app.state.auth_settings = auth_settings
    app.state.realtime_redis = None
    app.state.realtime_redis_lock = asyncio.Lock()
    app.state.project_operations = (
        project_operations
        if project_operations is not None
        else (
            ProjectOperationLocks()
            if persistence is not None
            else project_operation_locks_from_environment()
        )
    )
    app.state.task_dispatcher = task_dispatcher or CeleryTaskDispatcher()
    app.state.reconciliation_settings = (
        reconciliation_settings or QueuedReconciliationSettings.from_environment()
    )
    app.state.agent_workspace = AgentSnapshotWorkspace()
    register_error_handlers(app)
    app.include_router(projects_router)
    app.include_router(tasks_router)
    app.include_router(agent_runs_router)
    app.include_router(auth_router)

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
