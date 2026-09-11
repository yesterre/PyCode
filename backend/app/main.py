"""Run with: python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from backend.app.config import load_environment
from backend.app.db.session import DatabaseSessionManager
from backend.app.api.errors import register_error_handlers
from backend.app.api.projects import router
from backend.app.infrastructure.artifacts import SnapshotArtifactStore
from backend.app.infrastructure.projects import ProjectOperationLocks
from backend.app.infrastructure.repositories import DEFAULT_GIT_HOSTS, GitRepositorySource, RepositoryWorkspace
from backend.app.integrations.pycode import PyCodeAdapter
from backend.app.repositories import InMemoryPersistence


def create_app(*, adapter: PyCodeAdapter | None = None,
               workspace: RepositoryWorkspace | None = None,
               artifacts: SnapshotArtifactStore | None = None,
               persistence: InMemoryPersistence | None = None,
               database_url: str | None = None) -> FastAPI:
    """Use PostgreSQL by default; tests may explicitly inject memory persistence."""
    load_environment()
    database = DatabaseSessionManager(database_url) if persistence is None else None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        if database is not None:
            database.dispose()

    app = FastAPI(title="PyCode Backend", version="2.0-phase4", lifespan=lifespan)
    if workspace is None:
        hosts = os.environ.get("PYCODE_GIT_ALLOWED_HOSTS")
        source = GitRepositorySource(
            allowed_hosts={h.strip().lower() for h in hosts.split(",") if h.strip()} if hosts is not None else DEFAULT_GIT_HOSTS,
            timeout=float(os.environ.get("PYCODE_GIT_TIMEOUT_SECONDS", "120")),
        )
        workspace = RepositoryWorkspace(Path(os.environ.get("PYCODE_WORKSPACE_ROOT", ".pycode-workspaces")), source)
    app.state.workspace = workspace
    app.state.artifacts = (
        artifacts if artifacts is not None else SnapshotArtifactStore(workspace.root)
    )
    app.state.adapter = adapter if adapter is not None else PyCodeAdapter()
    app.state.persistence = persistence
    app.state.database = database
    app.state.project_operations = ProjectOperationLocks()
    register_error_handlers(app)
    app.include_router(router)

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
