from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from collections.abc import Callable
from threading import RLock
from typing import Any, Iterator
from uuid import UUID, uuid4

from backend.app.core.errors import BackendError
from backend.app.repositories.interfaces import METRIC_TASK_STATUSES, METRIC_TASK_TYPES, TaskMetricRow
from backend.app.core.models import (
    AgentCoreStart, AgentCoreStartStatus, AgentRun, AgentRunStatus,
    BackgroundTask, BackgroundTaskClaim, BackgroundTaskCreation,
    BackgroundTaskClaimStatus, BackgroundTaskStatus, IndexSummary,
    Project, ProjectSnapshot, ProjectStatus, TraceEvent, User, UserStatus,
    normalize_account_name, validate_user_fields,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryPersistence:
    """Shared test persistence. Production application never selects this by default."""

    def __init__(self) -> None:
        self.guard = RLock()
        self.users: dict[UUID, User] = {}
        self.projects: dict[UUID, Project] = {}
        self.snapshots: dict[UUID, ProjectSnapshot] = {}
        self.agent_runs: dict[UUID, AgentRun] = {}
        self.trace_events: dict[UUID, TraceEvent] = {}
        self.background_tasks: dict[UUID, BackgroundTask] = {}


class InMemoryUserRepository:
    def __init__(self, state: InMemoryPersistence) -> None:
        self.state = state

    def create(
        self, account_name: str, password_hash: str, *,
        status: UserStatus = UserStatus.ACTIVE,
    ) -> User:
        account_name = normalize_account_name(account_name)
        status = validate_user_fields(password_hash, status)
        if any(user.account_name == account_name for user in self.state.users.values()):
            raise BackendError("database_conflict", "A persistent record conflicts with existing data.")
        user = User(uuid4(), account_name, password_hash, _now(), status)
        self.state.users[user.id] = user
        return user

    def get(self, user_id: UUID) -> User:
        try:
            return self.state.users[user_id]
        except KeyError:
            raise BackendError("user_not_found", "User does not exist.") from None

    def get_by_account_name(self, account_name: str) -> User | None:
        normalized = normalize_account_name(account_name)
        return next((user for user in self.state.users.values()
                     if user.account_name == normalized), None)


class InMemoryProjectRepository:
    def __init__(self, state: InMemoryPersistence) -> None:
        self.state = state

    def create(
        self, name: str, repo_url: str, branch: str | None = None, *,
        owner_id: UUID | None = None,
    ) -> Project:
        if owner_id is not None and owner_id not in self.state.users:
            raise BackendError("database_conflict", "A related persistent record is invalid.")
        now = _now()
        project = Project(
            uuid4(), name, repo_url, branch, ProjectStatus.CREATED, now, now,
            owner_id=owner_id,
        )
        self.state.projects[project.id] = project
        return project

    def assign_legacy_owner(self, project_id: UUID, owner_id: UUID) -> Project:
        """Trusted maintenance operation; never reassign an owned project."""
        project = self.get(project_id)
        user = InMemoryUserRepository(self.state).get(owner_id)
        if project.owner_id is not None or user.status != UserStatus.ACTIVE:
            raise BackendError("database_conflict", "Legacy ownership assignment is not allowed.")
        updated = replace(project, owner_id=owner_id, updated_at=_now())
        self.state.projects[project_id] = updated
        return updated

    def get(self, project_id: UUID, *, for_update: bool = False) -> Project:
        # The UnitOfWork transaction holds the shared persistence RLock.
        try:
            return self.state.projects[project_id]
        except KeyError:
            raise BackendError("project_not_found", "Project does not exist.") from None

    def list_page(self, limit: int, offset: int) -> list[Project]:
        ordered = sorted(self.state.projects.values(),
                         key=lambda project: (project.updated_at, str(project.id)), reverse=True)
        return ordered[offset:offset + limit]

    def get_owned(self, project_id: UUID, owner_id: UUID, *, for_update: bool = False) -> Project:
        project = self.state.projects.get(project_id)
        if owner_id is None or project is None or project.owner_id != owner_id:
            raise BackendError("project_not_found", "Project does not exist.")
        return project

    def list_owned_page(
        self, owner_id: UUID, limit: int, offset: int, *, archived: bool | None = False,
    ) -> list[Project]:
        if owner_id is None:
            return []
        ordered = sorted(
            (project for project in self.state.projects.values() if project.owner_id == owner_id
             and (archived is None or (project.archived_at is not None) == archived)),
            key=lambda project: (project.updated_at, str(project.id)), reverse=True,
        )
        return ordered[offset:offset + limit]

    def set_archived(self, project_id: UUID, owner_id: UUID, *, archived: bool) -> Project:
        project = self.get_owned(project_id, owner_id, for_update=True)
        if (project.archived_at is not None) == archived:
            return project
        now = _now()
        updated = replace(project, archived_at=now if archived else None, updated_at=now)
        self.state.projects[project_id] = updated
        return updated

    def set_status(
        self, project_id: UUID, status: ProjectStatus, *,
        workspace_path: str | None = None, error: str | None = None,
    ) -> Project:
        project = self.get(project_id)
        updated = replace(
            project,
            status=status,
            workspace_path=workspace_path if workspace_path is not None else project.workspace_path,
            index_summary=None,
            last_error=error,
            updated_at=_now(),
        )
        self.state.projects[project_id] = updated
        return updated

    def set_current_snapshot(
        self, project_id: UUID, snapshot_id: UUID | None,
    ) -> Project:
        project = self.get(project_id)
        if snapshot_id is not None:
            snapshot = self.state.snapshots.get(snapshot_id)
            if (snapshot is None or snapshot.project_id != project_id
                    or snapshot.status != "ready"):
                raise BackendError(
                    "database_conflict", "A related persistent record is invalid.",
                )
        updated = replace(
            project, current_snapshot_id=snapshot_id, updated_at=_now(),
        )
        self.state.projects[project_id] = updated
        return updated


class InMemorySnapshotRepository:
    def __init__(self, state: InMemoryPersistence) -> None:
        self.state = state

    def get(self, snapshot_id: UUID) -> ProjectSnapshot:
        try:
            return self.state.snapshots[snapshot_id]
        except KeyError:
            raise BackendError("snapshot_not_found", "Project snapshot does not exist.") from None

    def get_for_project(self, project_id: UUID, snapshot_id: UUID) -> ProjectSnapshot:
        snapshot = self.state.snapshots.get(snapshot_id)
        if snapshot is None or snapshot.project_id != project_id:
            raise BackendError("snapshot_not_found", "Project snapshot does not exist.")
        return snapshot

    def get_by_commit(
        self, project_id: UUID, commit_sha: str,
    ) -> ProjectSnapshot | None:
        return next((
            snapshot for snapshot in self.state.snapshots.values()
            if snapshot.project_id == project_id and snapshot.commit_sha == commit_sha
        ), None)

    def start(self, project_id: UUID, commit_sha: str) -> ProjectSnapshot:
        self._project(project_id)
        existing = self.get_by_commit(project_id, commit_sha)
        now = _now()
        snapshot = ProjectSnapshot(
            existing.id if existing else uuid4(), project_id, commit_sha,
            None, None, None, None, None, "indexing",
            existing.created_at if existing else now, now,
        )
        self.state.snapshots[snapshot.id] = snapshot
        return snapshot

    def mark_ready(
        self, snapshot_id: UUID, *, index_artifact_path: str,
        graph_artifact_path: str, summary: IndexSummary,
    ) -> ProjectSnapshot:
        snapshot = replace(
            self.get(snapshot_id), status="ready",
            index_artifact_path=index_artifact_path,
            graph_artifact_path=graph_artifact_path,
            file_count=summary.file_count, node_count=summary.node_count,
            edge_count=summary.edge_count, error_message=None, updated_at=_now(),
        )
        self.state.snapshots[snapshot.id] = snapshot
        return snapshot

    def mark_failed(
        self, snapshot_id: UUID, error_message: str,
    ) -> ProjectSnapshot:
        snapshot = replace(
            self.get(snapshot_id), status="failed", error_message=error_message,
            index_artifact_path=None, graph_artifact_path=None,
            file_count=None, node_count=None, edge_count=None, updated_at=_now(),
        )
        self.state.snapshots[snapshot.id] = snapshot
        return snapshot

    def upsert(
        self, project_id: UUID, commit_sha: str, *,
        index_artifact_path: str | None, graph_artifact_path: str | None,
        summary: IndexSummary, status: str = "ready",
    ) -> ProjectSnapshot:
        self._project(project_id)
        existing = next((s for s in self.state.snapshots.values()
                         if s.project_id == project_id and s.commit_sha == commit_sha), None)
        now = _now()
        snapshot = ProjectSnapshot(
            existing.id if existing else uuid4(), project_id, commit_sha,
            index_artifact_path, graph_artifact_path,
            summary.file_count, summary.node_count, summary.edge_count,
            status, existing.created_at if existing else now, now,
        )
        self.state.snapshots[snapshot.id] = snapshot
        return snapshot

    def latest_ready(self, project_id: UUID) -> ProjectSnapshot | None:
        values = [s for s in self.state.snapshots.values()
                  if s.project_id == project_id and s.status == "ready"]
        return max(values, key=lambda s: (s.updated_at, str(s.id)), default=None)

    def list_for_project(self, project_id: UUID) -> list[ProjectSnapshot]:
        return sorted(
            (s for s in self.state.snapshots.values() if s.project_id == project_id),
            key=lambda s: (s.updated_at, str(s.id)), reverse=True,
        )

    def list_recent(self, project_id: UUID, limit: int) -> list[ProjectSnapshot]:
        return sorted(
            (s for s in self.state.snapshots.values() if s.project_id == project_id),
            key=lambda s: (s.created_at, str(s.id)), reverse=True,
        )[:limit]

    def _project(self, project_id: UUID) -> Project:
        return InMemoryProjectRepository(self.state).get(project_id)


class InMemoryAgentRunRepository:
    def __init__(self, state: InMemoryPersistence) -> None:
        self.state = state

    def create(
        self, project_id: UUID, snapshot_id: UUID | None, *,
        task: str, run_type: str, model: str | None,
    ) -> AgentRun:
        return self._create(
            project_id, snapshot_id, task=task, run_type=run_type, model=model,
            status=AgentRunStatus.RUNNING,
        )

    def create_queued(
        self, project_id: UUID, snapshot_id: UUID | None, *,
        task: str, run_type: str, model: str | None,
    ) -> AgentRun:
        return self._create(
            project_id, snapshot_id, task=task, run_type=run_type, model=model,
            status=AgentRunStatus.QUEUED,
        )

    def _create(
        self, project_id: UUID, snapshot_id: UUID | None, *,
        task: str, run_type: str, model: str | None, status: AgentRunStatus,
    ) -> AgentRun:
        InMemoryProjectRepository(self.state).get(project_id)
        if snapshot_id is not None and snapshot_id not in self.state.snapshots:
            raise BackendError("database_conflict", "A related persistent record is invalid.")
        now = _now()
        run = AgentRun(
            uuid4(), project_id, snapshot_id, task, run_type, status,
            model, None, None, None, None,
            now if status == AgentRunStatus.RUNNING else None, None, now,
        )
        self.state.agent_runs[run.id] = run
        return run

    def get(self, run_id: UUID) -> AgentRun:
        try:
            return self.state.agent_runs[run_id]
        except KeyError:
            raise BackendError("agent_run_not_found", "Agent run does not exist.") from None

    def get_owned(self, run_id: UUID, owner_id: UUID) -> AgentRun:
        run = self.state.agent_runs.get(run_id)
        project = self.state.projects.get(run.project_id) if run is not None else None
        if owner_id is None or run is None or project is None or project.owner_id != owner_id:
            raise BackendError("agent_run_not_found", "Agent run does not exist.")
        return run

    def mark_running(self, run_id: UUID) -> AgentRun:
        run = replace(
            self._expect(run_id, AgentRunStatus.QUEUED),
            status=AgentRunStatus.RUNNING, started_at=_now(), finished_at=None,
            result=None, error_message=None,
        )
        self.state.agent_runs[run.id] = run
        return run

    def requeue_running(self, run_id: UUID) -> AgentRun:
        run = self._expect(run_id, AgentRunStatus.RUNNING)
        if run.core_started_at is not None:
            raise BackendError(
                "agent_run_state_conflict",
                "Agent run cannot make the requested state transition.",
            )
        queued = replace(
            run, status=AgentRunStatus.QUEUED, started_at=None,
            finished_at=None, result=None, error_message=None,
        )
        self.state.agent_runs[run_id] = queued
        return queued

    def mark_core_started(self, run_id: UUID) -> AgentCoreStart:
        run = self.state.agent_runs.get(run_id)
        if run is None:
            return AgentCoreStart(AgentCoreStartStatus.NOT_FOUND, None)
        if run.core_started_at is not None:
            return AgentCoreStart(AgentCoreStartStatus.ALREADY_MARKED, run)
        if run.status != AgentRunStatus.RUNNING:
            return AgentCoreStart(AgentCoreStartStatus.INVALID_STATE, run)
        marked = replace(run, core_started_at=_now())
        self.state.agent_runs[run_id] = marked
        return AgentCoreStart(AgentCoreStartStatus.MARKED, marked)

    def complete(
        self, run_id: UUID, result: str | None, *,
        prompt_tokens: int | None = None, completion_tokens: int | None = None,
    ) -> AgentRun:
        run = replace(
            self._expect(run_id, AgentRunStatus.RUNNING),
            status=AgentRunStatus.COMPLETED, result=result,
            error_message=None, prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens, finished_at=_now(),
        )
        self.state.agent_runs[run.id] = run
        return run

    def fail(self, run_id: UUID, error_message: str) -> AgentRun:
        run = replace(
            self._expect(run_id, AgentRunStatus.RUNNING),
            status=AgentRunStatus.FAILED, result=None,
            error_message=error_message, finished_at=_now(),
        )
        self.state.agent_runs[run.id] = run
        return run

    def list_for_project(self, project_id: UUID) -> list[AgentRun]:
        return sorted(
            (run for run in self.state.agent_runs.values() if run.project_id == project_id),
            key=lambda run: (run.created_at, str(run.id)), reverse=True,
        )

    def list_history_for_project(
        self, project_id: UUID, limit: int, offset: int,
    ) -> list[AgentRun]:
        eligible = sorted(
            (
                run for run in self.state.agent_runs.values()
                if run.project_id == project_id
                and run.status == AgentRunStatus.COMPLETED
                and run.result is not None
                and run.run_type in {"ask", "impact", "agent"}
            ),
            key=lambda run: (run.created_at, str(run.id)), reverse=True,
        )
        return eligible[offset:offset + limit]

    def _expect(self, run_id: UUID, expected: AgentRunStatus) -> AgentRun:
        run = self.get(run_id)
        if run.status != expected:
            raise BackendError(
                "agent_run_state_conflict",
                "Agent run cannot make the requested state transition.",
            )
        return run


class InMemoryTraceEventRepository:
    def __init__(self, state: InMemoryPersistence) -> None:
        self.state = state

    def create(
        self, run_id: UUID, sequence: int, event_type: str, *,
        tool_name: str | None = None, payload: dict[str, Any] | None = None,
    ) -> TraceEvent:
        if run_id not in self.state.agent_runs:
            raise BackendError("database_conflict", "A related persistent record is invalid.")
        if any(event.run_id == run_id and event.sequence == sequence
               for event in self.state.trace_events.values()):
            raise BackendError("database_conflict", "A persistent record conflicts with existing data.")
        event = TraceEvent(uuid4(), run_id, sequence, event_type, tool_name, payload or {}, _now())
        self.state.trace_events[event.id] = event
        return event

    def list_for_run(self, run_id: UUID) -> list[TraceEvent]:
        return sorted(
            (event for event in self.state.trace_events.values() if event.run_id == run_id),
            key=lambda event: event.sequence,
        )


class InMemoryBackgroundTaskRepository:
    def metrics_snapshot(self) -> list[TaskMetricRow]:
        now, groups = _now(), {}
        for task in self.state.background_tasks.values():
            kind = task.task_type if task.task_type in METRIC_TASK_TYPES else "other"
            status = task.status if task.status in METRIC_TASK_STATUSES else "other"
            row = groups.setdefault((kind, status), dict(
                task_type=kind, status=status, count=0, started_count=0, wait_sum=0.0, oldest_age=0.0,
            ))
            row["count"] += 1
            if task.started_at is not None:
                row["started_count"] += 1
                row["wait_sum"] += max(0.0, (task.started_at - task.created_at).total_seconds())
            if status == "queued":
                row["oldest_age"] = max(row["oldest_age"], (now - task.created_at).total_seconds())
        return list(groups.values())

    def lock_owner_and_count_active(self, owner_id: UUID) -> int:
        # InMemoryUnitOfWork already holds the shared persistence RLock.
        InMemoryUserRepository(self.state).get(owner_id)
        return sum(
            task.status in {BackgroundTaskStatus.QUEUED, BackgroundTaskStatus.RUNNING}
            and task.project_id in self.state.projects
            and self.state.projects[task.project_id].owner_id == owner_id
            for task in self.state.background_tasks.values()
        )

    def __init__(self, state: InMemoryPersistence) -> None:
        self.state = state

    def create(
        self, task_type: str, *, project_id: UUID | None = None,
        snapshot_id: UUID | None = None, agent_run_id: UUID | None = None,
        idempotency_key: str | None = None, request_fingerprint: str | None = None,
        next_enqueue_attempt_at: datetime | None = None,
    ) -> BackgroundTask:
        if project_id is not None:
            return self.create_submission(
                task_type,
                project_id=project_id,
                snapshot_id=snapshot_id,
                agent_run_id=agent_run_id,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                next_enqueue_attempt_at=next_enqueue_attempt_at,
            ).task
        self._validate_idempotency_pair(idempotency_key, request_fingerprint)
        return self._insert(
            task_type,
            project_id=project_id,
            snapshot_id=snapshot_id,
            agent_run_id=agent_run_id,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            next_enqueue_attempt_at=next_enqueue_attempt_at,
        )

    def create_submission(
        self, task_type: str, *, project_id: UUID,
        snapshot_id: UUID | None = None, agent_run_id: UUID | None = None,
        idempotency_key: str | None = None, request_fingerprint: str | None = None,
        next_enqueue_attempt_at: datetime | None = None,
    ) -> BackgroundTaskCreation:
        self._validate_idempotency_pair(idempotency_key, request_fingerprint)
        if idempotency_key is not None:
            existing = next((
                task for task in self.state.background_tasks.values()
                if task.task_type == task_type
                and task.project_id == project_id
                and task.idempotency_key == idempotency_key
            ), None)
            if existing is not None:
                if existing.request_fingerprint != request_fingerprint:
                    raise BackendError(
                        "idempotency_conflict",
                        "The Idempotency-Key was already used for a different request.",
                    )
                return BackgroundTaskCreation(existing, False)
        if task_type == "repository_index":
            active = next((
                task for task in self.state.background_tasks.values()
                if task.task_type == task_type
                and task.project_id == project_id
                and task.status in {
                    BackgroundTaskStatus.QUEUED, BackgroundTaskStatus.RUNNING,
                }
            ), None)
            if active is not None:
                raise BackendError(
                    "repository_index_in_progress",
                    "An index operation is already queued or running for this project.",
                )
        task = self._insert(
            task_type,
            project_id=project_id,
            snapshot_id=snapshot_id,
            agent_run_id=agent_run_id,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            next_enqueue_attempt_at=next_enqueue_attempt_at,
        )
        return BackgroundTaskCreation(task, True)

    def _insert(
        self, task_type: str, *, project_id: UUID | None,
        snapshot_id: UUID | None, agent_run_id: UUID | None,
        idempotency_key: str | None, request_fingerprint: str | None,
        next_enqueue_attempt_at: datetime | None,
    ) -> BackgroundTask:
        if project_id is not None:
            InMemoryProjectRepository(self.state).get(project_id)
        if snapshot_id is not None and snapshot_id not in self.state.snapshots:
            raise BackendError("database_conflict", "A related persistent record is invalid.")
        if agent_run_id is not None and agent_run_id not in self.state.agent_runs:
            raise BackendError("database_conflict", "A related persistent record is invalid.")
        now = _now()
        task = BackgroundTask(
            uuid4(), task_type, BackgroundTaskStatus.QUEUED,
            project_id, snapshot_id, agent_run_id, 0, None, None,
            now, None, None, idempotency_key, request_fingerprint,
            0, None, None, next_enqueue_attempt_at or now,
        )
        self.state.background_tasks[task.id] = task
        return task

    def get(self, task_id: UUID) -> BackgroundTask:
        try:
            return self.state.background_tasks[task_id]
        except KeyError:
            raise BackendError(
                "background_task_not_found", "Background task does not exist.",
            ) from None

    def get_owned(self, task_id: UUID, owner_id: UUID) -> BackgroundTask:
        task = self.state.background_tasks.get(task_id)
        project = self.state.projects.get(task.project_id) if task is not None else None
        if owner_id is None or task is None or project is None or project.owner_id != owner_id:
            raise BackendError("background_task_not_found", "Background task does not exist.")
        return task

    def link_agent_run(
        self, task_id: UUID, agent_run_id: UUID, snapshot_id: UUID,
    ) -> BackgroundTask:
        task = self.get(task_id)
        if (
            task.task_type != "agent_run"
            or task.agent_run_id is not None
            or agent_run_id not in self.state.agent_runs
            or snapshot_id not in self.state.snapshots
        ):
            raise BackendError(
                "database_conflict", "A related persistent record is invalid.",
            )
        updated = replace(
            task, agent_run_id=agent_run_id, snapshot_id=snapshot_id,
        )
        self.state.background_tasks[task_id] = updated
        return updated

    @staticmethod
    def _validate_idempotency_pair(
        idempotency_key: str | None, request_fingerprint: str | None,
    ) -> None:
        if (idempotency_key is None) != (request_fingerprint is None):
            raise BackendError(
                "database_conflict", "A persistent record conflicts with existing data.",
            )

    def claim(
        self, task_id: UUID, *, lease_ttl_seconds: float = 420.0,
        worker_id: str | None = None,
    ) -> BackgroundTaskClaim:
        task = self.state.background_tasks.get(task_id)
        if task is None:
            return BackgroundTaskClaim(BackgroundTaskClaimStatus.NOT_FOUND, None)
        if task.status == BackgroundTaskStatus.RUNNING:
            return BackgroundTaskClaim(
                BackgroundTaskClaimStatus.ALREADY_RUNNING, task,
            )
        if task.status in {
            BackgroundTaskStatus.COMPLETED, BackgroundTaskStatus.FAILED,
        }:
            return BackgroundTaskClaim(BackgroundTaskClaimStatus.TERMINAL, task)
        now = _now()
        updated = replace(
            task, status=BackgroundTaskStatus.RUNNING,
            attempt_count=task.attempt_count + 1, started_at=now,
            error_code=None, error_message=None, execution_token=uuid4(),
            last_heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=lease_ttl_seconds),
            worker_id=worker_id,
        )
        self.state.background_tasks[task_id] = updated
        return BackgroundTaskClaim(BackgroundTaskClaimStatus.CLAIMED, updated)

    def heartbeat(
        self, task_id: UUID, execution_token: UUID, *, lease_ttl_seconds: float,
    ) -> BackgroundTask | None:
        task = self.state.background_tasks.get(task_id)
        if (
            task is None
            or task.status != BackgroundTaskStatus.RUNNING
            or task.execution_token != execution_token
        ):
            return None
        now = _now()
        updated = replace(
            task, last_heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=lease_ttl_seconds),
        )
        self.state.background_tasks[task_id] = updated
        return updated

    def guard_execution(
        self, task_id: UUID, execution_token: UUID,
    ) -> BackgroundTask | None:
        task = self.state.background_tasks.get(task_id)
        if (
            task is None
            or task.status != BackgroundTaskStatus.RUNNING
            or task.execution_token != execution_token
        ):
            return None
        return task

    def bind_snapshot_if_owner(
        self, task_id: UUID, execution_token: UUID, snapshot_id: UUID,
    ) -> BackgroundTask | None:
        task = self.guard_execution(task_id, execution_token)
        snapshot = self.state.snapshots.get(snapshot_id)
        if task is None or snapshot is None or snapshot.project_id != task.project_id:
            return None
        if task.snapshot_id is not None and task.snapshot_id != snapshot_id:
            return None
        updated = replace(task, snapshot_id=snapshot_id)
        self.state.background_tasks[task_id] = updated
        return updated

    def list_stale_running(self, batch_size: int) -> list[BackgroundTask]:
        now = _now()
        return sorted((
            task for task in self.state.background_tasks.values()
            if task.status == BackgroundTaskStatus.RUNNING
            and (task.lease_expires_at is None or task.lease_expires_at < now)
        ), key=lambda task: (task.lease_expires_at or task.started_at, str(task.id)))[:batch_size]

    def _stale_owner(
        self, task_id: UUID, execution_token: UUID | None,
    ) -> BackgroundTask | None:
        task = self.state.background_tasks.get(task_id)
        if task is None or task.status != BackgroundTaskStatus.RUNNING:
            return None
        if task.execution_token != execution_token:
            return None
        if task.lease_expires_at is not None and task.lease_expires_at >= _now():
            return None
        return task

    def requeue_if_stale(
        self, task_id: UUID, execution_token: UUID | None,
    ) -> BackgroundTask | None:
        task = self._stale_owner(task_id, execution_token)
        if task is None:
            return None
        updated = replace(
            task, status=BackgroundTaskStatus.QUEUED,
            started_at=None, finished_at=None, execution_token=None,
            lease_expires_at=None, last_heartbeat_at=None, worker_id=None,
            error_code=None, error_message=None, enqueue_confirmed_at=None,
            next_enqueue_attempt_at=_now(),
        )
        self.state.background_tasks[task_id] = updated
        return updated

    def complete_if_stale(
        self, task_id: UUID, execution_token: UUID | None,
    ) -> BackgroundTask | None:
        task = self._stale_owner(task_id, execution_token)
        if task is None:
            return None
        updated = replace(
            task, status=BackgroundTaskStatus.COMPLETED, finished_at=_now(),
            lease_expires_at=None, error_code=None, error_message=None,
        )
        self.state.background_tasks[task_id] = updated
        return updated

    def fail_if_stale(
        self, task_id: UUID, execution_token: UUID | None, *,
        error_code: str, error_message: str,
    ) -> BackgroundTask | None:
        task = self._stale_owner(task_id, execution_token)
        if task is None:
            return None
        updated = replace(
            task, status=BackgroundTaskStatus.FAILED, finished_at=_now(),
            lease_expires_at=None, error_code=error_code,
            error_message=error_message,
        )
        self.state.background_tasks[task_id] = updated
        return updated

    def reserve_enqueue_attempt(
        self, task_id: UUID, attempted_at: datetime,
        next_attempt_at: Callable[[int], datetime],
    ) -> BackgroundTask:
        task = self.get(task_id)
        if (
            task.status != BackgroundTaskStatus.QUEUED
            or task.enqueue_confirmed_at is not None
        ):
            raise BackendError(
                "background_task_state_conflict",
                "Background task cannot make the requested state transition.",
            )
        attempt = task.enqueue_attempt_count + 1
        updated = replace(
            task,
            enqueue_attempt_count=attempt,
            last_enqueue_attempt_at=attempted_at,
            next_enqueue_attempt_at=next_attempt_at(attempt),
        )
        self.state.background_tasks[task_id] = updated
        return updated

    def reserve_queued_enqueue_candidates(
        self, now: datetime, batch_size: int,
        next_attempt_at: Callable[[int], datetime],
    ) -> list[BackgroundTask]:
        candidates = sorted(
            (
                task for task in self.state.background_tasks.values()
                if task.status == BackgroundTaskStatus.QUEUED
                and task.enqueue_confirmed_at is None
                and task.next_enqueue_attempt_at is not None
                and task.next_enqueue_attempt_at <= now
            ),
            key=lambda task: (
                task.next_enqueue_attempt_at, task.created_at, str(task.id),
            ),
        )[:batch_size]
        return [
            self.reserve_enqueue_attempt(task.id, now, next_attempt_at)
            for task in candidates
        ]

    def mark_enqueue_confirmed(
        self, task_id: UUID, confirmed_at: datetime,
    ) -> BackgroundTask:
        task = self.get(task_id)
        updated = replace(
            task,
            enqueue_confirmed_at=confirmed_at,
            next_enqueue_attempt_at=None,
        )
        self.state.background_tasks[task_id] = updated
        return updated

    def mark_running(self, task_id: UUID) -> BackgroundTask:
        claim = self.claim(task_id)
        if claim.status != BackgroundTaskClaimStatus.CLAIMED or claim.task is None:
            if claim.status == BackgroundTaskClaimStatus.NOT_FOUND:
                return self.get(task_id)
            raise BackendError(
                "background_task_state_conflict",
                "Background task cannot make the requested state transition.",
            )
        return claim.task

    def mark_completed(
        self, task_id: UUID, *, snapshot_id: UUID | None = None,
        execution_token: UUID | None = None,
    ) -> BackgroundTask | None:
        task = self.get(task_id)
        if execution_token is None:
            task = self._expect(task_id, BackgroundTaskStatus.RUNNING)
        elif (
            task.status != BackgroundTaskStatus.RUNNING
            or task.execution_token != execution_token
        ):
            return None
        if snapshot_id is not None:
            snapshot = self.state.snapshots.get(snapshot_id)
            if (snapshot is None or task.project_id is None
                    or snapshot.project_id != task.project_id
                    or snapshot.status != "ready"):
                raise BackendError(
                    "database_conflict", "A related persistent record is invalid.",
                )
        updated = replace(
            task, status=BackgroundTaskStatus.COMPLETED, finished_at=_now(),
            snapshot_id=snapshot_id if snapshot_id is not None else task.snapshot_id,
            error_code=None, error_message=None, lease_expires_at=None,
        )
        self.state.background_tasks[task_id] = updated
        return updated

    def mark_failed(
        self, task_id: UUID, *, error_code: str, error_message: str,
        execution_token: UUID | None = None,
    ) -> BackgroundTask | None:
        task = self.get(task_id)
        if execution_token is None:
            task = self._expect(task_id, BackgroundTaskStatus.RUNNING)
        elif (
            task.status != BackgroundTaskStatus.RUNNING
            or task.execution_token != execution_token
        ):
            return None
        updated = replace(
            task, status=BackgroundTaskStatus.FAILED, finished_at=_now(),
            error_code=error_code, error_message=error_message,
            lease_expires_at=None,
        )
        self.state.background_tasks[task_id] = updated
        return updated

    def _expect(
        self, task_id: UUID, expected: BackgroundTaskStatus,
    ) -> BackgroundTask:
        task = self.get(task_id)
        if task.status != expected:
            raise BackendError(
                "background_task_state_conflict",
                "Background task cannot make the requested state transition.",
            )
        return task


class InMemoryUnitOfWork:
    def __init__(self, state: InMemoryPersistence) -> None:
        self.state = state
        self.users = InMemoryUserRepository(state)
        self.projects = InMemoryProjectRepository(state)
        self.snapshots = InMemorySnapshotRepository(state)
        self.agent_runs = InMemoryAgentRunRepository(state)
        self.trace_events = InMemoryTraceEventRepository(state)
        self.background_tasks = InMemoryBackgroundTaskRepository(state)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.state.guard:
            before = (
                deepcopy(self.state.users),
                deepcopy(self.state.projects), deepcopy(self.state.snapshots),
                deepcopy(self.state.agent_runs), deepcopy(self.state.trace_events),
                deepcopy(self.state.background_tasks),
            )
            try:
                yield
            except Exception:
                (self.state.users, self.state.projects, self.state.snapshots,
                 self.state.agent_runs, self.state.trace_events,
                 self.state.background_tasks) = before
                raise
