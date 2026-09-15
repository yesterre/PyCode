from __future__ import annotations

import os
from pathlib import Path

from backend.app.config import RepositoryRetrySettings, load_environment
from backend.app.infrastructure.artifacts import SnapshotArtifactStore
from backend.app.infrastructure.agent_workspace import AgentSnapshotWorkspace
from backend.app.infrastructure.projects import (
    ProjectOperationLocks, RedisProjectLockBackend,
)
from backend.app.infrastructure.repositories import (
    DEFAULT_GIT_HOSTS, GitRepositorySource, RepositoryWorkspace,
)
from backend.app.integrations.pycode import PyCodeAdapter
from backend.app.repositories import UnitOfWork
from backend.app.services.projects import ProjectService
from backend.app.services.agent_runs import AgentRunService
from backend.app.services.background_tasks import BackgroundTaskService


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
        retry_settings=RepositoryRetrySettings.from_environment(),
    )
    return RepositoryWorkspace(
        Path(os.environ.get("PYCODE_WORKSPACE_ROOT", ".pycode-workspaces")), source,
    )


def project_operation_locks_from_environment() -> ProjectOperationLocks:
    """Build the shared Redis project-lock namespace without connecting eagerly."""
    load_environment()
    ttl = float(os.environ.get("PYCODE_PROJECT_LOCK_TTL_SECONDS", "300"))
    raw_renewal = os.environ.get("PYCODE_PROJECT_LOCK_RENEW_SECONDS", "").strip()
    renewal = float(raw_renewal) if raw_renewal else None
    return ProjectOperationLocks(
        RedisProjectLockBackend(),
        ttl_seconds=ttl,
        renewal_interval_seconds=renewal,
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


def build_agent_run_service(
    unit_of_work: UnitOfWork,
    *,
    background_tasks: BackgroundTaskService,
    projects: ProjectService,
    adapter: PyCodeAdapter,
    agent_workspace: AgentSnapshotWorkspace,
) -> AgentRunService:
    return AgentRunService(
        unit_of_work,
        background_tasks,
        projects,
        adapter,
        agent_workspace,
    )
