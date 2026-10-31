from __future__ import annotations

from contextlib import AbstractContextManager
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from backend.app.core.models import (
    AgentCoreStart, AgentRun, BackgroundTask, BackgroundTaskClaim,
    BackgroundTaskCreation, IndexSummary, Project, ProjectSnapshot,
    ProjectStatus, TraceEvent, User, UserStatus,
)


class UserRepository(Protocol):
    def create(
        self, account_name: str, password_hash: str, *,
        status: UserStatus = UserStatus.ACTIVE,
    ) -> User: ...

    def get(self, user_id: UUID) -> User: ...

    def get_by_account_name(self, account_name: str) -> User | None: ...


class ProjectRepository(Protocol):
    def create(
        self, name: str, repo_url: str, branch: str | None = None, *,
        owner_id: UUID | None = None,
    ) -> Project: ...

    def assign_legacy_owner(self, project_id: UUID, owner_id: UUID) -> Project: ...

    def get(self, project_id: UUID, *, for_update: bool = False) -> Project: ...

    def list_page(self, limit: int, offset: int) -> list[Project]: ...

    def get_owned(self, project_id: UUID, owner_id: UUID, *, for_update: bool = False) -> Project: ...

    def list_owned_page(
        self, owner_id: UUID, limit: int, offset: int, *, archived: bool | None = False,
    ) -> list[Project]: ...

    def set_archived(self, project_id: UUID, owner_id: UUID, *, archived: bool) -> Project: ...

    def set_status(
        self, project_id: UUID, status: ProjectStatus, *,
        workspace_path: str | None = None, error: str | None = None,
    ) -> Project: ...

    def set_current_snapshot(
        self, project_id: UUID, snapshot_id: UUID | None,
    ) -> Project: ...


class ProjectSnapshotRepository(Protocol):
    def get(self, snapshot_id: UUID) -> ProjectSnapshot: ...

    def get_for_project(self, project_id: UUID, snapshot_id: UUID) -> ProjectSnapshot: ...

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

    def list_recent(self, project_id: UUID, limit: int) -> list[ProjectSnapshot]: ...


class AgentRunRepository(Protocol):
    def create(
        self, project_id: UUID, snapshot_id: UUID | None, *,
        task: str, run_type: str, model: str | None,
    ) -> AgentRun: ...

    def create_queued(
        self, project_id: UUID, snapshot_id: UUID | None, *,
        task: str, run_type: str, model: str | None,
    ) -> AgentRun: ...

    def get(self, run_id: UUID) -> AgentRun: ...

    def get_owned(self, run_id: UUID, owner_id: UUID) -> AgentRun: ...

    def mark_running(self, run_id: UUID) -> AgentRun: ...

    def requeue_running(self, run_id: UUID) -> AgentRun: ...

    def mark_core_started(self, run_id: UUID) -> AgentCoreStart: ...

    def complete(
        self, run_id: UUID, result: str | None, *,
        prompt_tokens: int | None = None, completion_tokens: int | None = None,
    ) -> AgentRun: ...

    def fail(self, run_id: UUID, error_message: str) -> AgentRun: ...

    def list_for_project(self, project_id: UUID) -> list[AgentRun]: ...

    def list_history_for_project(
        self, project_id: UUID, limit: int, offset: int,
    ) -> list[AgentRun]: ...


class TraceEventRepository(Protocol):
    def create(
        self, run_id: UUID, sequence: int, event_type: str, *,
        tool_name: str | None = None, payload: dict[str, Any] | None = None,
    ) -> TraceEvent: ...

    def list_for_run(self, run_id: UUID) -> list[TraceEvent]: ...


class BackgroundTaskRepository(Protocol):
    def lock_owner_and_count_active(self, owner_id: UUID) -> int: ...

    def create(
        self, task_type: str, *, project_id: UUID | None = None,
        snapshot_id: UUID | None = None, agent_run_id: UUID | None = None,
        idempotency_key: str | None = None, request_fingerprint: str | None = None,
        next_enqueue_attempt_at: datetime | None = None,
    ) -> BackgroundTask: ...

    def create_submission(
        self, task_type: str, *, project_id: UUID,
        snapshot_id: UUID | None = None, agent_run_id: UUID | None = None,
        idempotency_key: str | None = None, request_fingerprint: str | None = None,
        next_enqueue_attempt_at: datetime | None = None,
    ) -> BackgroundTaskCreation: ...

    def get(self, task_id: UUID) -> BackgroundTask: ...

    def get_owned(self, task_id: UUID, owner_id: UUID) -> BackgroundTask: ...

    def link_agent_run(
        self, task_id: UUID, agent_run_id: UUID, snapshot_id: UUID,
    ) -> BackgroundTask: ...

    def claim(
        self, task_id: UUID, *, lease_ttl_seconds: float = 420.0,
        worker_id: str | None = None,
    ) -> BackgroundTaskClaim: ...

    def heartbeat(
        self, task_id: UUID, execution_token: UUID, *, lease_ttl_seconds: float,
    ) -> BackgroundTask | None: ...

    def guard_execution(
        self, task_id: UUID, execution_token: UUID,
    ) -> BackgroundTask | None: ...

    def bind_snapshot_if_owner(
        self, task_id: UUID, execution_token: UUID, snapshot_id: UUID,
    ) -> BackgroundTask | None: ...

    def list_stale_running(self, batch_size: int) -> list[BackgroundTask]: ...

    def requeue_if_stale(
        self, task_id: UUID, execution_token: UUID | None,
    ) -> BackgroundTask | None: ...

    def complete_if_stale(
        self, task_id: UUID, execution_token: UUID | None,
    ) -> BackgroundTask | None: ...

    def fail_if_stale(
        self, task_id: UUID, execution_token: UUID | None, *,
        error_code: str, error_message: str,
    ) -> BackgroundTask | None: ...

    def reserve_enqueue_attempt(
        self, task_id: UUID, attempted_at: datetime,
        next_attempt_at: Callable[[int], datetime],
    ) -> BackgroundTask: ...

    def reserve_queued_enqueue_candidates(
        self, now: datetime, batch_size: int,
        next_attempt_at: Callable[[int], datetime],
    ) -> list[BackgroundTask]: ...

    def mark_enqueue_confirmed(
        self, task_id: UUID, confirmed_at: datetime,
    ) -> BackgroundTask: ...

    def mark_running(self, task_id: UUID) -> BackgroundTask: ...

    def mark_completed(
        self, task_id: UUID, *, snapshot_id: UUID | None = None,
        execution_token: UUID | None = None,
    ) -> BackgroundTask | None: ...

    def mark_failed(
        self, task_id: UUID, *, error_code: str, error_message: str,
        execution_token: UUID | None = None,
    ) -> BackgroundTask | None: ...


class UnitOfWork(Protocol):
    users: UserRepository
    projects: ProjectRepository
    snapshots: ProjectSnapshotRepository
    agent_runs: AgentRunRepository
    trace_events: TraceEventRepository
    background_tasks: BackgroundTaskRepository

    def transaction(self) -> AbstractContextManager[None]: ...
