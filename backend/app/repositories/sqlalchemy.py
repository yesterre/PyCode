from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Iterator
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.core.errors import BackendError
from backend.app.core.models import (
    AgentCoreStart, AgentCoreStartStatus, AgentRun, AgentRunStatus,
    BackgroundTask, BackgroundTaskClaim, BackgroundTaskCreation,
    BackgroundTaskClaimStatus, BackgroundTaskStatus, IndexSummary,
    Project, ProjectSnapshot, ProjectStatus, TraceEvent,
)
from backend.app.db.models import (
    AgentRunORM, BackgroundTaskORM, ProjectORM, ProjectSnapshotORM, TraceEventORM,
)


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
        now = _now()
        record = AgentRunORM(
            id=uuid4(), project_id=project_id, snapshot_id=snapshot_id,
            task=task, run_type=run_type, status=status.value,
            model=model,
            started_at=now if status == AgentRunStatus.RUNNING else None,
            created_at=now,
        )
        self.session.add(record)
        self.session.flush()
        return _agent_run(record)

    def get(self, run_id: UUID) -> AgentRun:
        record = self.session.get(AgentRunORM, run_id)
        if record is None:
            raise BackendError("agent_run_not_found", "Agent run does not exist.")
        return _agent_run(record)

    def mark_running(self, run_id: UUID) -> AgentRun:
        record = self._expect(run_id, AgentRunStatus.QUEUED)
        record.status = AgentRunStatus.RUNNING.value
        record.started_at = _now()
        record.finished_at = None
        record.result = None
        record.error_message = None
        self.session.flush()
        return _agent_run(record)

    def mark_core_started(self, run_id: UUID) -> AgentCoreStart:
        statement = (
            update(AgentRunORM)
            .where(
                AgentRunORM.id == run_id,
                AgentRunORM.status == AgentRunStatus.RUNNING.value,
                AgentRunORM.core_started_at.is_(None),
            )
            .values(core_started_at=_now())
            .returning(AgentRunORM)
            .execution_options(populate_existing=True)
        )
        record = self.session.execute(statement).scalar_one_or_none()
        if record is not None:
            return AgentCoreStart(AgentCoreStartStatus.MARKED, _agent_run(record))
        record = self.session.get(AgentRunORM, run_id, populate_existing=True)
        if record is None:
            return AgentCoreStart(AgentCoreStartStatus.NOT_FOUND, None)
        if record.core_started_at is not None:
            status = AgentCoreStartStatus.ALREADY_MARKED
        else:
            status = AgentCoreStartStatus.INVALID_STATE
        return AgentCoreStart(status, _agent_run(record))

    def complete(
        self, run_id: UUID, result: str | None, *,
        prompt_tokens: int | None = None, completion_tokens: int | None = None,
    ) -> AgentRun:
        record = self._expect(run_id, AgentRunStatus.RUNNING)
        record.status = AgentRunStatus.COMPLETED.value
        record.result = result
        record.error_message = None
        record.prompt_tokens = prompt_tokens
        record.completion_tokens = completion_tokens
        record.finished_at = _now()
        self.session.flush()
        return _agent_run(record)

    def fail(self, run_id: UUID, error_message: str) -> AgentRun:
        record = self._expect(run_id, AgentRunStatus.RUNNING)
        record.status = AgentRunStatus.FAILED.value
        record.result = None
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

    def _expect(self, run_id: UUID, expected: AgentRunStatus) -> AgentRunORM:
        record = self._get_record(run_id)
        if record.status != expected.value:
            raise BackendError(
                "agent_run_state_conflict",
                "Agent run cannot make the requested state transition.",
            )
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


class SqlAlchemyBackgroundTaskRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

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
        now = _now()
        record = BackgroundTaskORM(
            id=uuid4(), task_type=task_type,
            status=BackgroundTaskStatus.QUEUED.value,
            project_id=project_id, snapshot_id=snapshot_id,
            agent_run_id=agent_run_id, attempt_count=0, created_at=now,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            enqueue_attempt_count=0,
            next_enqueue_attempt_at=next_enqueue_attempt_at or now,
        )
        self.session.add(record)
        self.session.flush()
        return _background_task(record)

    def create_submission(
        self, task_type: str, *, project_id: UUID,
        snapshot_id: UUID | None = None, agent_run_id: UUID | None = None,
        idempotency_key: str | None = None, request_fingerprint: str | None = None,
        next_enqueue_attempt_at: datetime | None = None,
    ) -> BackgroundTaskCreation:
        if (idempotency_key is None) != (request_fingerprint is None):
            raise BackendError(
                "database_conflict", "A persistent record conflicts with existing data.",
            )
        now = _now()
        statement = (
            insert(BackgroundTaskORM)
            .values(
                id=uuid4(), task_type=task_type,
                status=BackgroundTaskStatus.QUEUED.value,
                project_id=project_id, snapshot_id=snapshot_id,
                agent_run_id=agent_run_id, attempt_count=0,
                error_code=None, error_message=None,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                enqueue_attempt_count=0,
                last_enqueue_attempt_at=None,
                enqueue_confirmed_at=None,
                next_enqueue_attempt_at=next_enqueue_attempt_at or now,
                created_at=now, started_at=None, finished_at=None,
            )
            .on_conflict_do_nothing()
            .returning(BackgroundTaskORM)
            .execution_options(populate_existing=True)
        )
        record = self.session.scalars(statement).one_or_none()
        if record is not None:
            self.session.flush()
            return BackgroundTaskCreation(_background_task(record), True)

        if idempotency_key is not None:
            existing = self._get_by_idempotency_scope(
                task_type, project_id, idempotency_key,
            )
            if existing is not None:
                if existing.request_fingerprint != request_fingerprint:
                    raise BackendError(
                        "idempotency_conflict",
                        "The Idempotency-Key was already used for a different request.",
                    )
                return BackgroundTaskCreation(existing, False)

        if task_type == "repository_index":
            active = self._get_active_repository_index(project_id)
            if active is not None:
                raise BackendError(
                    "repository_index_in_progress",
                    "An index operation is already queued or running for this project.",
                )
        raise BackendError(
            "database_conflict", "A persistent record conflicts with existing data.",
        )

    def get(self, task_id: UUID) -> BackgroundTask:
        return _background_task(self._get_record(task_id))

    def link_agent_run(
        self, task_id: UUID, agent_run_id: UUID, snapshot_id: UUID,
    ) -> BackgroundTask:
        statement = (
            update(BackgroundTaskORM)
            .where(
                BackgroundTaskORM.id == task_id,
                BackgroundTaskORM.task_type == "agent_run",
                BackgroundTaskORM.agent_run_id.is_(None),
            )
            .values(agent_run_id=agent_run_id, snapshot_id=snapshot_id)
            .returning(BackgroundTaskORM)
            .execution_options(populate_existing=True)
        )
        record = self.session.scalars(statement).one_or_none()
        if record is None:
            raise BackendError(
                "database_conflict", "A related persistent record is invalid.",
            )
        self.session.flush()
        return _background_task(record)

    def _get_by_idempotency_scope(
        self, task_type: str, project_id: UUID, idempotency_key: str,
    ) -> BackgroundTask | None:
        statement = select(BackgroundTaskORM).where(
            BackgroundTaskORM.task_type == task_type,
            BackgroundTaskORM.project_id == project_id,
            BackgroundTaskORM.idempotency_key == idempotency_key,
        )
        record = self.session.scalars(statement).one_or_none()
        return _background_task(record) if record is not None else None

    def _get_active_repository_index(
        self, project_id: UUID,
    ) -> BackgroundTask | None:
        statement = (
            select(BackgroundTaskORM)
            .where(
                BackgroundTaskORM.task_type == "repository_index",
                BackgroundTaskORM.project_id == project_id,
                BackgroundTaskORM.status.in_((
                    BackgroundTaskStatus.QUEUED.value,
                    BackgroundTaskStatus.RUNNING.value,
                )),
            )
            .order_by(BackgroundTaskORM.created_at, BackgroundTaskORM.id)
            .limit(1)
        )
        record = self.session.scalars(statement).first()
        return _background_task(record) if record is not None else None

    def claim(self, task_id: UUID) -> BackgroundTaskClaim:
        statement = (
            update(BackgroundTaskORM)
            .where(
                BackgroundTaskORM.id == task_id,
                BackgroundTaskORM.status == BackgroundTaskStatus.QUEUED.value,
            )
            .values(
                status=BackgroundTaskStatus.RUNNING.value,
                attempt_count=BackgroundTaskORM.attempt_count + 1,
                started_at=_now(),
                finished_at=None,
                error_code=None,
                error_message=None,
            )
            .returning(BackgroundTaskORM)
            .execution_options(populate_existing=True)
        )
        record = self.session.scalars(statement).one_or_none()
        if record is not None:
            self.session.flush()
            return BackgroundTaskClaim(
                BackgroundTaskClaimStatus.CLAIMED, _background_task(record),
            )

        record = self.session.get(BackgroundTaskORM, task_id, populate_existing=True)
        if record is None:
            return BackgroundTaskClaim(BackgroundTaskClaimStatus.NOT_FOUND, None)
        task = _background_task(record)
        if task.status == BackgroundTaskStatus.RUNNING:
            return BackgroundTaskClaim(
                BackgroundTaskClaimStatus.ALREADY_RUNNING, task,
            )
        return BackgroundTaskClaim(BackgroundTaskClaimStatus.TERMINAL, task)

    def reserve_enqueue_attempt(
        self, task_id: UUID, attempted_at: datetime,
        next_attempt_at: Callable[[int], datetime],
    ) -> BackgroundTask:
        record = self._get_record(task_id)
        if (
            record.status != BackgroundTaskStatus.QUEUED.value
            or record.enqueue_confirmed_at is not None
        ):
            raise BackendError(
                "background_task_state_conflict",
                "Background task cannot make the requested state transition.",
            )
        attempt = record.enqueue_attempt_count + 1
        record.enqueue_attempt_count = attempt
        record.last_enqueue_attempt_at = attempted_at
        record.next_enqueue_attempt_at = next_attempt_at(attempt)
        self.session.flush()
        return _background_task(record)

    def reserve_queued_enqueue_candidates(
        self, now: datetime, batch_size: int,
        next_attempt_at: Callable[[int], datetime],
    ) -> list[BackgroundTask]:
        statement = (
            select(BackgroundTaskORM)
            .where(
                BackgroundTaskORM.status == BackgroundTaskStatus.QUEUED.value,
                BackgroundTaskORM.enqueue_confirmed_at.is_(None),
                BackgroundTaskORM.next_enqueue_attempt_at.is_not(None),
                BackgroundTaskORM.next_enqueue_attempt_at <= now,
            )
            .order_by(
                BackgroundTaskORM.next_enqueue_attempt_at,
                BackgroundTaskORM.created_at,
                BackgroundTaskORM.id,
            )
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        records = list(self.session.scalars(statement))
        for record in records:
            attempt = record.enqueue_attempt_count + 1
            record.enqueue_attempt_count = attempt
            record.last_enqueue_attempt_at = now
            record.next_enqueue_attempt_at = next_attempt_at(attempt)
        self.session.flush()
        return [_background_task(record) for record in records]

    def mark_enqueue_confirmed(
        self, task_id: UUID, confirmed_at: datetime,
    ) -> BackgroundTask:
        record = self._get_record(task_id)
        record.enqueue_confirmed_at = confirmed_at
        record.next_enqueue_attempt_at = None
        self.session.flush()
        return _background_task(record)

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
    ) -> BackgroundTask:
        record = self._expect(task_id, BackgroundTaskStatus.RUNNING)
        if snapshot_id is not None:
            snapshot = self.session.get(ProjectSnapshotORM, snapshot_id)
            if (snapshot is None or record.project_id is None
                    or snapshot.project_id != record.project_id
                    or snapshot.status != "ready"):
                raise BackendError(
                    "database_conflict", "A related persistent record is invalid.",
                )
            record.snapshot_id = snapshot_id
        record.status = BackgroundTaskStatus.COMPLETED.value
        record.finished_at = _now()
        record.error_code = None
        record.error_message = None
        self.session.flush()
        return _background_task(record)

    def mark_failed(
        self, task_id: UUID, *, error_code: str, error_message: str,
    ) -> BackgroundTask:
        record = self._expect(task_id, BackgroundTaskStatus.RUNNING)
        record.status = BackgroundTaskStatus.FAILED.value
        record.error_code = error_code
        record.error_message = error_message
        record.finished_at = _now()
        self.session.flush()
        return _background_task(record)

    def _get_record(self, task_id: UUID) -> BackgroundTaskORM:
        record = self.session.get(BackgroundTaskORM, task_id)
        if record is None:
            raise BackendError(
                "background_task_not_found", "Background task does not exist.",
            )
        return record

    def _expect(
        self, task_id: UUID, expected: BackgroundTaskStatus,
    ) -> BackgroundTaskORM:
        record = self._get_record(task_id)
        if record.status != expected.value:
            raise BackendError(
                "background_task_state_conflict",
                "Background task cannot make the requested state transition.",
            )
        return record


class SqlAlchemyUnitOfWork:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.projects = SqlAlchemyProjectRepository(session)
        self.snapshots = SqlAlchemyProjectSnapshotRepository(session)
        self.agent_runs = SqlAlchemyAgentRunRepository(session)
        self.trace_events = SqlAlchemyTraceEventRepository(session)
        self.background_tasks = SqlAlchemyBackgroundTaskRepository(session)

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
        core_started_at=record.core_started_at,
    )


def _trace_event(record: TraceEventORM) -> TraceEvent:
    return TraceEvent(
        record.id, record.run_id, record.sequence, record.event_type,
        record.tool_name, dict(record.payload), record.created_at,
    )


def _background_task(record: BackgroundTaskORM) -> BackgroundTask:
    return BackgroundTask(
        record.id, record.task_type, BackgroundTaskStatus(record.status),
        record.project_id, record.snapshot_id, record.agent_run_id,
        record.attempt_count, record.error_code, record.error_message,
        record.created_at, record.started_at, record.finished_at,
        record.idempotency_key, record.request_fingerprint,
        record.enqueue_attempt_count, record.last_enqueue_attempt_at,
        record.enqueue_confirmed_at, record.next_enqueue_attempt_at,
    )
