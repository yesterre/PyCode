from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Iterator
from uuid import UUID, uuid4

from backend.app.core.errors import BackendError
from backend.app.core.models import (
    AgentRun, AgentRunStatus, IndexSummary, Project, ProjectSnapshot, ProjectStatus, TraceEvent,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryPersistence:
    """Shared test persistence. Production application never selects this by default."""

    def __init__(self) -> None:
        self.guard = RLock()
        self.projects: dict[UUID, Project] = {}
        self.snapshots: dict[UUID, ProjectSnapshot] = {}
        self.agent_runs: dict[UUID, AgentRun] = {}
        self.trace_events: dict[UUID, TraceEvent] = {}


class InMemoryProjectRepository:
    def __init__(self, state: InMemoryPersistence) -> None:
        self.state = state

    def create(self, name: str, repo_url: str, branch: str | None = None) -> Project:
        now = _now()
        project = Project(uuid4(), name, repo_url, branch, ProjectStatus.CREATED, now, now)
        self.state.projects[project.id] = project
        return project

    def get(self, project_id: UUID) -> Project:
        try:
            return self.state.projects[project_id]
        except KeyError:
            raise BackendError("project_not_found", "Project does not exist.") from None

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

    def _project(self, project_id: UUID) -> Project:
        return InMemoryProjectRepository(self.state).get(project_id)


class InMemoryAgentRunRepository:
    def __init__(self, state: InMemoryPersistence) -> None:
        self.state = state

    def create(
        self, project_id: UUID, snapshot_id: UUID | None, *,
        task: str, run_type: str, model: str | None,
    ) -> AgentRun:
        InMemoryProjectRepository(self.state).get(project_id)
        if snapshot_id is not None and snapshot_id not in self.state.snapshots:
            raise BackendError("database_conflict", "A related persistent record is invalid.")
        now = _now()
        run = AgentRun(
            uuid4(), project_id, snapshot_id, task, run_type, AgentRunStatus.RUNNING,
            model, None, None, None, None, now, None, now,
        )
        self.state.agent_runs[run.id] = run
        return run

    def get(self, run_id: UUID) -> AgentRun:
        try:
            return self.state.agent_runs[run_id]
        except KeyError:
            raise BackendError("agent_run_not_found", "Agent run does not exist.") from None

    def complete(
        self, run_id: UUID, result: str, *,
        prompt_tokens: int | None = None, completion_tokens: int | None = None,
    ) -> AgentRun:
        run = replace(
            self.get(run_id), status=AgentRunStatus.COMPLETED, result=result,
            error_message=None, prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens, finished_at=_now(),
        )
        self.state.agent_runs[run.id] = run
        return run

    def fail(self, run_id: UUID, error_message: str) -> AgentRun:
        run = replace(
            self.get(run_id), status=AgentRunStatus.FAILED,
            error_message=error_message, finished_at=_now(),
        )
        self.state.agent_runs[run.id] = run
        return run

    def list_for_project(self, project_id: UUID) -> list[AgentRun]:
        return sorted(
            (run for run in self.state.agent_runs.values() if run.project_id == project_id),
            key=lambda run: (run.created_at, str(run.id)), reverse=True,
        )


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


class InMemoryUnitOfWork:
    def __init__(self, state: InMemoryPersistence) -> None:
        self.state = state
        self.projects = InMemoryProjectRepository(state)
        self.snapshots = InMemorySnapshotRepository(state)
        self.agent_runs = InMemoryAgentRunRepository(state)
        self.trace_events = InMemoryTraceEventRepository(state)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.state.guard:
            before = (
                deepcopy(self.state.projects), deepcopy(self.state.snapshots),
                deepcopy(self.state.agent_runs), deepcopy(self.state.trace_events),
            )
            try:
                yield
            except Exception:
                (self.state.projects, self.state.snapshots,
                 self.state.agent_runs, self.state.trace_events) = before
                raise
