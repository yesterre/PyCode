from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol
from uuid import UUID

from backend.app.core.models import (
    AgentRun, IndexSummary, Project, ProjectSnapshot, ProjectStatus, TraceEvent,
)


class ProjectRepository(Protocol):
    def create(self, name: str, repo_url: str, branch: str | None = None) -> Project: ...

    def get(self, project_id: UUID) -> Project: ...

    def set_status(
        self, project_id: UUID, status: ProjectStatus, *,
        workspace_path: str | None = None, error: str | None = None,
    ) -> Project: ...


class ProjectSnapshotRepository(Protocol):
    def upsert(
        self, project_id: UUID, commit_sha: str, *,
        index_artifact_path: str | None, graph_artifact_path: str | None,
        summary: IndexSummary, status: str = "ready",
    ) -> ProjectSnapshot: ...

    def latest_ready(self, project_id: UUID) -> ProjectSnapshot | None: ...

    def list_for_project(self, project_id: UUID) -> list[ProjectSnapshot]: ...


class AgentRunRepository(Protocol):
    def create(
        self, project_id: UUID, snapshot_id: UUID | None, *,
        task: str, run_type: str, model: str | None,
    ) -> AgentRun: ...

    def get(self, run_id: UUID) -> AgentRun: ...

    def complete(
        self, run_id: UUID, result: str, *,
        prompt_tokens: int | None = None, completion_tokens: int | None = None,
    ) -> AgentRun: ...

    def fail(self, run_id: UUID, error_message: str) -> AgentRun: ...

    def list_for_project(self, project_id: UUID) -> list[AgentRun]: ...


class TraceEventRepository(Protocol):
    def create(
        self, run_id: UUID, sequence: int, event_type: str, *,
        tool_name: str | None = None, payload: dict[str, Any] | None = None,
    ) -> TraceEvent: ...

    def list_for_run(self, run_id: UUID) -> list[TraceEvent]: ...


class UnitOfWork(Protocol):
    projects: ProjectRepository
    snapshots: ProjectSnapshotRepository
    agent_runs: AgentRunRepository
    trace_events: TraceEventRepository

    def transaction(self) -> AbstractContextManager[None]: ...
