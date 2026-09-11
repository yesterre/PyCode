from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.core.errors import BackendError
from backend.app.core.models import (
    AgentRun, AgentRunStatus, IndexSummary, Project, ProjectSnapshot, ProjectStatus, TraceEvent,
)
from backend.app.db.models import AgentRunORM, ProjectORM, ProjectSnapshotORM, TraceEventORM


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SqlAlchemyProjectRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, name: str, repo_url: str, branch: str | None = None) -> Project:
        now = _now()
        record = ProjectORM(
            id=uuid4(), name=name, repo_url=repo_url, branch=branch,
            status=ProjectStatus.CREATED.value, created_at=now, updated_at=now,
        )
        self.session.add(record)
        self.session.flush()
        return _project(record)

    def get(self, project_id: UUID) -> Project:
        record = self.session.get(ProjectORM, project_id)
        if record is None:
            raise BackendError("project_not_found", "Project does not exist.")
        return _project(record)

    def set_status(
        self, project_id: UUID, status: ProjectStatus, *,
        workspace_path: str | None = None, error: str | None = None,
    ) -> Project:
        record = self.session.get(ProjectORM, project_id)
        if record is None:
            raise BackendError("project_not_found", "Project does not exist.")
        record.status = status.value
        if workspace_path is not None:
            record.workspace_path = workspace_path
        record.last_error = error
        record.updated_at = _now()
        self.session.flush()
        return _project(record)

    def set_current_snapshot(
        self, project_id: UUID, snapshot_id: UUID | None,
    ) -> Project:
        record = self.session.get(ProjectORM, project_id)
        if record is None:
            raise BackendError("project_not_found", "Project does not exist.")
        if snapshot_id is not None:
            snapshot = self.session.get(ProjectSnapshotORM, snapshot_id)
            if (snapshot is None or snapshot.project_id != project_id
                    or snapshot.status != "ready"):
                raise BackendError(
                    "database_conflict", "A related persistent record is invalid.",
                )
        record.current_snapshot_id = snapshot_id
        record.updated_at = _now()
        self.session.flush()
        return _project(record)


class SqlAlchemyProjectSnapshotRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, snapshot_id: UUID) -> ProjectSnapshot:
        record = self.session.get(ProjectSnapshotORM, snapshot_id)
        if record is None:
            raise BackendError("snapshot_not_found", "Project snapshot does not exist.")
        return _snapshot(record)

    def get_by_commit(
        self, project_id: UUID, commit_sha: str,
    ) -> ProjectSnapshot | None:
        statement = select(ProjectSnapshotORM).where(
            ProjectSnapshotORM.project_id == project_id,
            ProjectSnapshotORM.commit_sha == commit_sha,
        )
        record = self.session.scalars(statement).one_or_none()
        return _snapshot(record) if record is not None else None

    def start(self, project_id: UUID, commit_sha: str) -> ProjectSnapshot:
        now = _now()
        values = {
            "id": uuid4(), "project_id": project_id, "commit_sha": commit_sha,
            "index_artifact_path": None, "graph_artifact_path": None,
            "file_count": None, "node_count": None, "edge_count": None,
            "status": "indexing", "error_message": None,
            "created_at": now, "updated_at": now,
        }
        statement = insert(ProjectSnapshotORM).values(**values).on_conflict_do_update(
            constraint="uq_project_snapshots_project_commit",
            set_={key: values[key] for key in (
                "index_artifact_path", "graph_artifact_path", "file_count",
                "node_count", "edge_count", "status", "error_message", "updated_at",
            )},
        ).returning(ProjectSnapshotORM).execution_options(populate_existing=True)
        record = self.session.scalars(statement).one()
        self.session.flush()
        return _snapshot(record)

    def mark_ready(
        self, snapshot_id: UUID, *, index_artifact_path: str,
        graph_artifact_path: str, summary: IndexSummary,
    ) -> ProjectSnapshot:
        record = self._get_record(snapshot_id)
        record.status = "ready"
        record.index_artifact_path = index_artifact_path
        record.graph_artifact_path = graph_artifact_path
        record.file_count = summary.file_count
        record.node_count = summary.node_count
        record.edge_count = summary.edge_count
        record.error_message = None
        record.updated_at = _now()
        self.session.flush()
        return _snapshot(record)

    def mark_failed(
        self, snapshot_id: UUID, error_message: str,
    ) -> ProjectSnapshot:
        record = self._get_record(snapshot_id)
        record.status = "failed"
        record.index_artifact_path = None
        record.graph_artifact_path = None
        record.file_count = None
        record.node_count = None
        record.edge_count = None
        record.error_message = error_message
        record.updated_at = _now()
        self.session.flush()
        return _snapshot(record)

    def _get_record(self, snapshot_id: UUID) -> ProjectSnapshotORM:
        record = self.session.get(ProjectSnapshotORM, snapshot_id)
        if record is None:
            raise BackendError("snapshot_not_found", "Project snapshot does not exist.")
        return record

    def upsert(
        self, project_id: UUID, commit_sha: str, *,
        index_artifact_path: str | None, graph_artifact_path: str | None,
        summary: IndexSummary, status: str = "ready",
    ) -> ProjectSnapshot:
        now = _now()
        values = {
            "id": uuid4(), "project_id": project_id, "commit_sha": commit_sha,
            "index_artifact_path": index_artifact_path,
            "graph_artifact_path": graph_artifact_path,
            "file_count": summary.file_count, "node_count": summary.node_count,
            "edge_count": summary.edge_count, "status": status,
            "created_at": now, "updated_at": now,
        }
        statement = insert(ProjectSnapshotORM).values(**values).on_conflict_do_update(
            constraint="uq_project_snapshots_project_commit",
            set_={key: values[key] for key in (
                "index_artifact_path", "graph_artifact_path", "file_count",
                "node_count", "edge_count", "status", "updated_at",
            )},
        ).returning(ProjectSnapshotORM).execution_options(populate_existing=True)
        record = self.session.scalars(statement).one()
        self.session.flush()
        return _snapshot(record)

    def latest_ready(self, project_id: UUID) -> ProjectSnapshot | None:
        statement = (
            select(ProjectSnapshotORM)
            .where(ProjectSnapshotORM.project_id == project_id, ProjectSnapshotORM.status == "ready")
            .order_by(ProjectSnapshotORM.updated_at.desc(), ProjectSnapshotORM.id.desc())
            .limit(1)
        )
        record = self.session.scalars(statement).first()
        return _snapshot(record) if record is not None else None

    def list_for_project(self, project_id: UUID) -> list[ProjectSnapshot]:
        statement = (
            select(ProjectSnapshotORM)
            .where(ProjectSnapshotORM.project_id == project_id)
            .order_by(ProjectSnapshotORM.updated_at.desc(), ProjectSnapshotORM.id.desc())
        )
        return [_snapshot(record) for record in self.session.scalars(statement)]


class SqlAlchemyAgentRunRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self, project_id: UUID, snapshot_id: UUID | None, *,
        task: str, run_type: str, model: str | None,
    ) -> AgentRun:
        now = _now()
        record = AgentRunORM(
            id=uuid4(), project_id=project_id, snapshot_id=snapshot_id,
            task=task, run_type=run_type, status=AgentRunStatus.RUNNING.value,
            model=model, started_at=now, created_at=now,
        )
        self.session.add(record)
        self.session.flush()
        return _agent_run(record)

    def get(self, run_id: UUID) -> AgentRun:
        record = self.session.get(AgentRunORM, run_id)
        if record is None:
            raise BackendError("agent_run_not_found", "Agent run does not exist.")
        return _agent_run(record)

    def complete(
        self, run_id: UUID, result: str, *,
        prompt_tokens: int | None = None, completion_tokens: int | None = None,
    ) -> AgentRun:
        record = self._get_record(run_id)
        record.status = AgentRunStatus.COMPLETED.value
        record.result = result
        record.error_message = None
        record.prompt_tokens = prompt_tokens
        record.completion_tokens = completion_tokens
        record.finished_at = _now()
        self.session.flush()
        return _agent_run(record)

    def fail(self, run_id: UUID, error_message: str) -> AgentRun:
        record = self._get_record(run_id)
        record.status = AgentRunStatus.FAILED.value
        record.error_message = error_message
        record.finished_at = _now()
        self.session.flush()
        return _agent_run(record)

    def list_for_project(self, project_id: UUID) -> list[AgentRun]:
        statement = (
            select(AgentRunORM)
            .where(AgentRunORM.project_id == project_id)
            .order_by(AgentRunORM.created_at.desc(), AgentRunORM.id.desc())
        )
        return [_agent_run(record) for record in self.session.scalars(statement)]

    def _get_record(self, run_id: UUID) -> AgentRunORM:
        record = self.session.get(AgentRunORM, run_id)
        if record is None:
            raise BackendError("agent_run_not_found", "Agent run does not exist.")
        return record


class SqlAlchemyTraceEventRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self, run_id: UUID, sequence: int, event_type: str, *,
        tool_name: str | None = None, payload: dict[str, Any] | None = None,
    ) -> TraceEvent:
        record = TraceEventORM(
            id=uuid4(), run_id=run_id, sequence=sequence, event_type=event_type,
            tool_name=tool_name, payload=payload or {}, created_at=_now(),
        )
        self.session.add(record)
        self.session.flush()
        return _trace_event(record)

    def list_for_run(self, run_id: UUID) -> list[TraceEvent]:
        statement = (
            select(TraceEventORM)
            .where(TraceEventORM.run_id == run_id)
            .order_by(TraceEventORM.sequence.asc())
        )
        return [_trace_event(record) for record in self.session.scalars(statement)]


class SqlAlchemyUnitOfWork:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.projects = SqlAlchemyProjectRepository(session)
        self.snapshots = SqlAlchemyProjectSnapshotRepository(session)
        self.agent_runs = SqlAlchemyAgentRunRepository(session)
        self.trace_events = SqlAlchemyTraceEventRepository(session)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        try:
            yield
            self.session.commit()
        except SQLAlchemyError as exc:
            self.session.rollback()
            raise _database_error(exc) from exc
        except Exception:
            self.session.rollback()
            raise


def _database_error(exc: SQLAlchemyError) -> BackendError:
    if isinstance(exc, OperationalError):
        return BackendError("database_unavailable", "The database is unavailable.")
    if isinstance(exc, IntegrityError):
        return BackendError("database_conflict", "A persistent record conflicts with existing data.")
    return BackendError("database_failed", "A database transaction failed.")


def _project(record: ProjectORM) -> Project:
    return Project(
        record.id, record.name, record.repo_url, record.branch,
        ProjectStatus(record.status), record.created_at, record.updated_at,
        workspace_path=record.workspace_path, last_error=record.last_error,
        current_snapshot_id=record.current_snapshot_id,
    )


def _snapshot(record: ProjectSnapshotORM) -> ProjectSnapshot:
    return ProjectSnapshot(
        record.id, record.project_id, record.commit_sha,
        record.index_artifact_path, record.graph_artifact_path,
        record.file_count, record.node_count, record.edge_count,
        record.status, record.created_at, record.updated_at,
        error_message=record.error_message,
    )


def _agent_run(record: AgentRunORM) -> AgentRun:
    return AgentRun(
        record.id, record.project_id, record.snapshot_id, record.task, record.run_type,
        AgentRunStatus(record.status), record.model, record.prompt_tokens,
        record.completion_tokens, record.result, record.error_message,
        record.started_at, record.finished_at, record.created_at,
    )


def _trace_event(record: TraceEventORM) -> TraceEvent:
    return TraceEvent(
        record.id, record.run_id, record.sequence, record.event_type,
        record.tool_name, dict(record.payload), record.created_at,
    )
