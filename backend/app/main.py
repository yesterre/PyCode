"""Run with: python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000."""

import os
from pathlib import Path

from fastapi import FastAPI

from backend.app.api.errors import register_error_handlers
from backend.app.api.projects import router
from backend.app.infrastructure.projects import InMemoryProjectStore
from backend.app.infrastructure.repositories import DEFAULT_GIT_HOSTS, GitRepositorySource, RepositoryWorkspace
from backend.app.integrations.pycode import PyCodeAdapter
from backend.app.services.projects import ProjectService


def create_app(*, adapter: PyCodeAdapter | None = None,
               workspace: RepositoryWorkspace | None = None) -> FastAPI:
    """Own all project state in this instance; inject an adapter for offline tests."""
    app = FastAPI(title="PyCode Backend", version="2.0-phase2")
    store = InMemoryProjectStore()
    app.state.project_store = store
    if workspace is None:
        hosts = os.environ.get("PYCODE_GIT_ALLOWED_HOSTS")
        source = GitRepositorySource(
            allowed_hosts={h.strip().lower() for h in hosts.split(",") if h.strip()} if hosts is not None else DEFAULT_GIT_HOSTS,
            timeout=float(os.environ.get("PYCODE_GIT_TIMEOUT_SECONDS", "120")),
        )
        workspace = RepositoryWorkspace(Path(os.environ.get("PYCODE_WORKSPACE_ROOT", ".pycode-workspaces")), source)
    app.state.workspace = workspace
    app.state.project_service = ProjectService(
        store, adapter if adapter is not None else PyCodeAdapter(),
        workspace=workspace,
    )
    register_error_handlers(app)
    app.include_router(router)

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
