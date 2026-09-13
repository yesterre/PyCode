from __future__ import annotations

import os
from pathlib import Path

from backend.app.config import load_environment
from backend.app.infrastructure.artifacts import SnapshotArtifactStore
from backend.app.infrastructure.projects import ProjectOperationLocks
from backend.app.infrastructure.repositories import (
    DEFAULT_GIT_HOSTS, GitRepositorySource, RepositoryWorkspace,
)
from backend.app.integrations.pycode import PyCodeAdapter
from backend.app.repositories import UnitOfWork
from backend.app.services.projects import ProjectService


def repository_workspace_from_environment() -> RepositoryWorkspace:
    """Build the same managed Repository adapter for FastAPI and Worker."""
    load_environment()
    hosts = os.environ.get("PYCODE_GIT_ALLOWED_HOSTS")
    allowed_hosts = (
        {host.strip().lower() for host in hosts.split(",") if host.strip()}
        if hosts is not None else DEFAULT_GIT_HOSTS
    )
    source = GitRepositorySource(
        allowed_hosts=allowed_hosts,
        timeout=float(os.environ.get("PYCODE_GIT_TIMEOUT_SECONDS", "120")),
    )
    return RepositoryWorkspace(
        Path(os.environ.get("PYCODE_WORKSPACE_ROOT", ".pycode-workspaces")), source,
    )


def build_project_service(
    unit_of_work: UnitOfWork, *, adapter: PyCodeAdapter,
    workspace: RepositoryWorkspace, artifacts: SnapshotArtifactStore,
    operations: ProjectOperationLocks,
) -> ProjectService:
    return ProjectService(
        unit_of_work, adapter, workspace=workspace,
        operations=operations, artifacts=artifacts,
    )
