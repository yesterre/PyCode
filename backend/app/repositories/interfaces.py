from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol
from uuid import UUID

from backend.app.core.models import (
    AgentRun, BackgroundTask, IndexSummary, Project, ProjectSnapshot, ProjectStatus, TraceEvent,
)


class ProjectRepository(Protocol):
    def create(self, name: str, repo_url: str, branch: str | None = None) -> Project: ...

    def get(self, project_id: UUID) -> Project: ...

    def set_status(
        self, project_id: UUID, status: ProjectStatus, *,
        workspace_path: str | None = None, error: str | None = None,
    ) -> Project: ...

    def set_current_snapshot(
        self, project_id: UUID, snapshot_id: UUID | None,
    ) -> Project: ...


class ProjectSnapshotRepository(Protocol):
    def get(self, snapshot_id: UUID) -> ProjectSnapshot: ...

    def get_by_commit(
        self, project_id: UUID, commit_sha: str,
    ) -> ProjectSnapshot | None: ...

    def start(self, project_id: UUID, commit_sha: str) -> ProjectSnapshot: ...

    def mark_ready(
        self, snapshot_id: UUID, *, index_artifact_path: str,
        graph_artifact_path: str, summary: IndexSummary,
    ) -> ProjectSnapshot: ...

    def mark_failed(
        self, snapshot_id: UUID, error_message: str,
    ) -> ProjectSnapshot: ...

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


class BackgroundTaskRepository(Protocol):
    def create(
        self, task_type: str, *, project_id: UUID | None = None,
        snapshot_id: UUID | None = None, agent_run_id: UUID | None = None,
    ) -> BackgroundTask: ...

    def get(self, task_id: UUID) -> BackgroundTask: ...

    def mark_running(self, task_id: UUID) -> BackgroundTask: ...

    def mark_completed(
        self, task_id: UUID, *, snapshot_id: UUID | None = None,
    ) -> BackgroundTask: ...

    def mark_failed(
        self, task_id: UUID, *, error_code: str, error_message: str,
    ) -> BackgroundTask: ...


class UnitOfWork(Protocol):
    projects: ProjectRepository
    snapshots: ProjectSnapshotRepository
    agent_runs: AgentRunRepository
    trace_events: TraceEventRepository
    background_tasks: BackgroundTaskRepository

    def transaction(self) -> AbstractContextManager[None]: ...
