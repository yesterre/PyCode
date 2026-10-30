from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.core.errors import BackendError
from backend.app.core.models import (
    AgentCoreStart, AgentCoreStartStatus, AgentRun, AgentRunStatus,
    BackgroundTask, BackgroundTaskClaim, BackgroundTaskCreation,
    BackgroundTaskClaimStatus, BackgroundTaskStatus, IndexSummary,
    Project, ProjectSnapshot, ProjectStatus, TraceEvent, User, UserStatus,
    normalize_account_name, validate_user_fields,
)
from backend.app.db.models import (
    AgentRunORM, BackgroundTaskORM, ProjectORM, ProjectSnapshotORM, TraceEventORM, UserORM,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SqlAlchemyUserRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self, account_name: str, password_hash: str, *,
        status: UserStatus = UserStatus.ACTIVE,
    ) -> User:
        normalized = normalize_account_name(account_name)
        status = validate_user_fields(password_hash, status)
        record = UserORM(
            id=uuid4(), account_name=normalized, password_hash=password_hash,
            status=status.value, created_at=_now(),
        )
        self.session.add(record)
        self.session.flush()
        return _user(record)

    def get(self, user_id: UUID) -> User:
        record = self.session.get(UserORM, user_id)
        if record is None:
            raise BackendError("user_not_found", "User does not exist.")
        return _user(record)

    def get_by_account_name(self, account_name: str) -> User | None:
        normalized = normalize_account_name(account_name)
        record = self.session.scalars(
            select(UserORM).where(UserORM.account_name == normalized),
        ).one_or_none()
        return _user(record) if record is not None else None


class SqlAlchemyProjectRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self, name: str, repo_url: str, branch: str | None = None, *,
        owner_id: UUID | None = None,
    ) -> Project:
        now = _now()
        record = ProjectORM(
            id=uuid4(), name=name, repo_url=repo_url, branch=branch,
            status=ProjectStatus.CREATED.value, created_at=now, updated_at=now,
            owner_id=owner_id,
        )
        self.session.add(record)
        self.session.flush()
        return _project(record)

    def assign_legacy_owner(self, project_id: UUID, owner_id: UUID) -> Project:
        """Lock the target user and compare-and-set only a legacy NULL owner."""
        self.get(project_id)
        user = self.session.scalars(
            select(UserORM).where(UserORM.id == owner_id).with_for_update(),
        ).one_or_none()
        if user is None:
            raise BackendError("user_not_found", "User does not exist.")
        if user.status != UserStatus.ACTIVE.value:
            raise BackendError("database_conflict", "Legacy ownership assignment is not allowed.")
        record = self.session.scalars(
            update(ProjectORM)
            .where(ProjectORM.id == project_id, ProjectORM.owner_id.is_(None))
            .values(owner_id=owner_id, updated_at=_now())
            .returning(ProjectORM).execution_options(populate_existing=True),
        ).one_or_none()
        if record is None:
            raise BackendError("database_conflict", "Legacy ownership assignment is not allowed.")
        return _project(record)

    def get(self, project_id: UUID, *, for_update: bool = False) -> Project:
        record = self.session.get(
            ProjectORM, project_id, with_for_update=for_update, populate_existing=for_update,
        )
        if record is None:
            raise BackendError("project_not_found", "Project does not exist.")
        return _project(record)

    def list_page(self, limit: int, offset: int) -> list[Project]:
        statement = (select(ProjectORM)
                     .order_by(ProjectORM.updated_at.desc(), ProjectORM.id.desc())
                     .limit(limit).offset(offset))
        return [_project(record) for record in self.session.scalars(statement)]

    def get_owned(self, project_id: UUID, owner_id: UUID, *, for_update: bool = False) -> Project:
        if owner_id is None:
            raise BackendError("project_not_found", "Project does not exist.")
        statement = select(ProjectORM).where(
            ProjectORM.id == project_id, ProjectORM.owner_id == owner_id,
        )
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        record = self.session.scalars(statement).one_or_none()
        if record is None:
            raise BackendError("project_not_found", "Project does not exist.")
        return _project(record)

    def list_owned_page(
        self, owner_id: UUID, limit: int, offset: int, *, archived: bool | None = False,
    ) -> list[Project]:
        if owner_id is None:
            return []
        statement = select(ProjectORM).where(ProjectORM.owner_id == owner_id)
        if archived is not None:
            statement = statement.where(
                ProjectORM.archived_at.is_not(None) if archived else ProjectORM.archived_at.is_(None),
            )
        statement = (
            statement
            .order_by(ProjectORM.updated_at.desc(), ProjectORM.id.desc())
            .limit(limit).offset(offset)
        )
        return [_project(record) for record in self.session.scalars(statement)]

    def set_archived(self, project_id: UUID, owner_id: UUID, *, archived: bool) -> Project:
        project = self.get_owned(project_id, owner_id, for_update=True)
        if (project.archived_at is not None) == archived:
            return project
        record = self.session.get(ProjectORM, project_id)
        now = _now()
        record.archived_at = now if archived else None
        record.updated_at = now
        self.session.flush()
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

    def get_for_project(self, project_id: UUID, snapshot_id: UUID) -> ProjectSnapshot:
        record = self.session.scalars(select(ProjectSnapshotORM).where(
            ProjectSnapshotORM.id == snapshot_id, ProjectSnapshotORM.project_id == project_id,
        )).one_or_none()
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

    def list_recent(self, project_id: UUID, limit: int) -> list[ProjectSnapshot]:
        statement = (select(ProjectSnapshotORM)
                     .where(ProjectSnapshotORM.project_id == project_id)
                     .order_by(ProjectSnapshotORM.created_at.desc(), ProjectSnapshotORM.id.desc())
                     .limit(limit))
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

    def get_owned(self, run_id: UUID, owner_id: UUID) -> AgentRun:
        if owner_id is None:
            raise BackendError("agent_run_not_found", "Agent run does not exist.")
        record = self.session.scalars(select(AgentRunORM).join(
            ProjectORM, AgentRunORM.project_id == ProjectORM.id,
        ).where(AgentRunORM.id == run_id, ProjectORM.owner_id == owner_id)).one_or_none()
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

    def requeue_running(self, run_id: UUID) -> AgentRun:
        statement = (
            update(AgentRunORM)
            .where(
                AgentRunORM.id == run_id,
                AgentRunORM.status == AgentRunStatus.RUNNING.value,
                AgentRunORM.core_started_at.is_(None),
            )
            .values(
                status=AgentRunStatus.QUEUED.value,
                started_at=None, finished_at=None, result=None,
                error_message=None,
            )
            .returning(AgentRunORM)
            .execution_options(populate_existing=True)
        )
        record = self.session.scalars(statement).one_or_none()
        if record is None:
            raise BackendError(
                "agent_run_state_conflict",
                "Agent run cannot make the requested state transition.",
            )
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

    def list_history_for_project(
        self, project_id: UUID, limit: int, offset: int,
    ) -> list[AgentRun]:
        statement = (
            select(AgentRunORM)
            .where(
                AgentRunORM.project_id == project_id,
                AgentRunORM.status == AgentRunStatus.COMPLETED.value,
                AgentRunORM.result.is_not(None),
                AgentRunORM.run_type.in_(("ask", "impact", "agent")),
            )
            .order_by(AgentRunORM.created_at.desc(), AgentRunORM.id.desc())
            .offset(offset)
            .limit(limit)
        )
        if offset > 0:
            # Skip narrow keys before fetching full rows; retain the first-page plan.
            page = statement.with_only_columns(AgentRunORM.id).subquery()
            statement = (
                select(AgentRunORM)
                .join(page, AgentRunORM.id == page.c.id)
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

    def get_owned(self, task_id: UUID, owner_id: UUID) -> BackgroundTask:
        if owner_id is None:
            raise BackendError("background_task_not_found", "Background task does not exist.")
        record = self.session.scalars(select(BackgroundTaskORM).join(
            ProjectORM, BackgroundTaskORM.project_id == ProjectORM.id,
        ).where(BackgroundTaskORM.id == task_id, ProjectORM.owner_id == owner_id)).one_or_none()
        if record is None:
            raise BackendError("background_task_not_found", "Background task does not exist.")
        return _background_task(record)

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

    def claim(
        self, task_id: UUID, *, lease_ttl_seconds: float = 420.0,
        worker_id: str | None = None,
    ) -> BackgroundTaskClaim:
        execution_token = uuid4()
        statement = (
            update(BackgroundTaskORM)
            .where(
                BackgroundTaskORM.id == task_id,
                BackgroundTaskORM.status == BackgroundTaskStatus.QUEUED.value,
            )
            .values(
                status=BackgroundTaskStatus.RUNNING.value,
                attempt_count=BackgroundTaskORM.attempt_count + 1,
                started_at=func.now(),
                finished_at=None,
                error_code=None,
                error_message=None,
                execution_token=execution_token,
                last_heartbeat_at=func.now(),
                lease_expires_at=func.now() + timedelta(seconds=lease_ttl_seconds),
                worker_id=worker_id,
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

    def heartbeat(
        self, task_id: UUID, execution_token: UUID, *, lease_ttl_seconds: float,
    ) -> BackgroundTask | None:
        statement = (
            update(BackgroundTaskORM)
            .where(
                BackgroundTaskORM.id == task_id,
                BackgroundTaskORM.status == BackgroundTaskStatus.RUNNING.value,
                BackgroundTaskORM.execution_token == execution_token,
            )
            .values(
                last_heartbeat_at=func.now(),
                lease_expires_at=func.now() + timedelta(seconds=lease_ttl_seconds),
            )
            .returning(BackgroundTaskORM)
            .execution_options(populate_existing=True)
        )
        record = self.session.scalars(statement).one_or_none()
        return _background_task(record) if record is not None else None

    def guard_execution(
        self, task_id: UUID, execution_token: UUID,
    ) -> BackgroundTask | None:
        statement = (
            select(BackgroundTaskORM)
            .where(
                BackgroundTaskORM.id == task_id,
                BackgroundTaskORM.status == BackgroundTaskStatus.RUNNING.value,
                BackgroundTaskORM.execution_token == execution_token,
            )
            .with_for_update()
        )
        record = self.session.scalars(statement).one_or_none()
        return _background_task(record) if record is not None else None

    def bind_snapshot_if_owner(
        self, task_id: UUID, execution_token: UUID, snapshot_id: UUID,
    ) -> BackgroundTask | None:
        snapshot = self.session.get(ProjectSnapshotORM, snapshot_id)
        if snapshot is None:
            return None
        statement = (
            update(BackgroundTaskORM)
            .where(
                BackgroundTaskORM.id == task_id,
                BackgroundTaskORM.status == BackgroundTaskStatus.RUNNING.value,
                BackgroundTaskORM.execution_token == execution_token,
                BackgroundTaskORM.project_id == snapshot.project_id,
                or_(
                    BackgroundTaskORM.snapshot_id.is_(None),
                    BackgroundTaskORM.snapshot_id == snapshot_id,
                ),
            )
            .values(snapshot_id=snapshot_id)
            .returning(BackgroundTaskORM)
            .execution_options(populate_existing=True)
        )
        record = self.session.scalars(statement).one_or_none()
        return _background_task(record) if record is not None else None

    def list_stale_running(self, batch_size: int) -> list[BackgroundTask]:
        statement = (
            select(BackgroundTaskORM)
            .where(
                BackgroundTaskORM.status == BackgroundTaskStatus.RUNNING.value,
                or_(
                    BackgroundTaskORM.lease_expires_at.is_(None),
                    BackgroundTaskORM.lease_expires_at < func.now(),
                ),
            )
            .order_by(
                BackgroundTaskORM.lease_expires_at.asc().nullsfirst(),
                BackgroundTaskORM.id,
            )
            .limit(batch_size)
        )
        return [_background_task(record) for record in self.session.scalars(statement)]

    @staticmethod
    def _stale_owner_conditions(
        task_id: UUID, execution_token: UUID | None,
    ) -> tuple[Any, ...]:
        token_condition = (
            BackgroundTaskORM.execution_token.is_(None)
            if execution_token is None
            else BackgroundTaskORM.execution_token == execution_token
        )
        return (
            BackgroundTaskORM.id == task_id,
            BackgroundTaskORM.status == BackgroundTaskStatus.RUNNING.value,
            token_condition,
            or_(
                BackgroundTaskORM.lease_expires_at.is_(None),
                BackgroundTaskORM.lease_expires_at < func.now(),
            ),
        )

    def requeue_if_stale(
        self, task_id: UUID, execution_token: UUID | None,
    ) -> BackgroundTask | None:
        statement = (
            update(BackgroundTaskORM)
            .where(*self._stale_owner_conditions(task_id, execution_token))
            .values(
                status=BackgroundTaskStatus.QUEUED.value,
                started_at=None, finished_at=None, execution_token=None,
                lease_expires_at=None, last_heartbeat_at=None, worker_id=None,
                error_code=None, error_message=None, enqueue_confirmed_at=None,
                next_enqueue_attempt_at=func.now(),
            )
            .returning(BackgroundTaskORM)
            .execution_options(populate_existing=True)
        )
        record = self.session.scalars(statement).one_or_none()
        return _background_task(record) if record is not None else None

    def complete_if_stale(
        self, task_id: UUID, execution_token: UUID | None,
    ) -> BackgroundTask | None:
        statement = (
            update(BackgroundTaskORM)
            .where(*self._stale_owner_conditions(task_id, execution_token))
            .values(
                status=BackgroundTaskStatus.COMPLETED.value,
                finished_at=func.now(), lease_expires_at=None,
                error_code=None, error_message=None,
            )
            .returning(BackgroundTaskORM)
            .execution_options(populate_existing=True)
        )
        record = self.session.scalars(statement).one_or_none()
        return _background_task(record) if record is not None else None

    def fail_if_stale(
        self, task_id: UUID, execution_token: UUID | None, *,
        error_code: str, error_message: str,
    ) -> BackgroundTask | None:
        statement = (
            update(BackgroundTaskORM)
            .where(*self._stale_owner_conditions(task_id, execution_token))
            .values(
                status=BackgroundTaskStatus.FAILED.value,
                finished_at=func.now(), lease_expires_at=None,
                error_code=error_code, error_message=error_message,
            )
            .returning(BackgroundTaskORM)
            .execution_options(populate_existing=True)
        )
        record = self.session.scalars(statement).one_or_none()
        return _background_task(record) if record is not None else None

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
        execution_token: UUID | None = None,
    ) -> BackgroundTask | None:
        if execution_token is not None:
            values: dict[str, Any] = {
                "status": BackgroundTaskStatus.COMPLETED.value,
                "finished_at": func.now(), "error_code": None,
                "error_message": None, "lease_expires_at": None,
            }
            if snapshot_id is not None:
                snapshot = self.session.get(ProjectSnapshotORM, snapshot_id)
                if snapshot is None:
                    raise BackendError(
                        "database_conflict", "A related persistent record is invalid.",
                    )
                values["snapshot_id"] = snapshot_id
            statement = (
                update(BackgroundTaskORM)
                .where(
                    BackgroundTaskORM.id == task_id,
                    BackgroundTaskORM.status == BackgroundTaskStatus.RUNNING.value,
                    BackgroundTaskORM.execution_token == execution_token,
                )
                .values(**values)
                .returning(BackgroundTaskORM)
                .execution_options(populate_existing=True)
            )
            record = self.session.scalars(statement).one_or_none()
            if record is None:
                return None
            if snapshot_id is not None and (
                record.project_id is None or snapshot.project_id != record.project_id
                or snapshot.status != "ready"
            ):
                raise BackendError(
                    "database_conflict", "A related persistent record is invalid.",
                )
            return _background_task(record)
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
        record.lease_expires_at = None
        self.session.flush()
        return _background_task(record)

    def mark_failed(
        self, task_id: UUID, *, error_code: str, error_message: str,
        execution_token: UUID | None = None,
    ) -> BackgroundTask | None:
        if execution_token is not None:
            statement = (
                update(BackgroundTaskORM)
                .where(
                    BackgroundTaskORM.id == task_id,
                    BackgroundTaskORM.status == BackgroundTaskStatus.RUNNING.value,
                    BackgroundTaskORM.execution_token == execution_token,
                )
                .values(
                    status=BackgroundTaskStatus.FAILED.value,
                    error_code=error_code, error_message=error_message,
                    finished_at=func.now(), lease_expires_at=None,
                )
                .returning(BackgroundTaskORM)
                .execution_options(populate_existing=True)
            )
            record = self.session.scalars(statement).one_or_none()
            return _background_task(record) if record is not None else None
        record = self._expect(task_id, BackgroundTaskStatus.RUNNING)
        record.status = BackgroundTaskStatus.FAILED.value
        record.error_code = error_code
        record.error_message = error_message
        record.finished_at = _now()
        record.lease_expires_at = None
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
        self.users = SqlAlchemyUserRepository(session)
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


def _user(record: UserORM) -> User:
    return User(
        record.id, record.account_name, record.password_hash,
        record.created_at, UserStatus(record.status),
    )


def _project(record: ProjectORM) -> Project:
    return Project(
        record.id, record.name, record.repo_url, record.branch,
        ProjectStatus(record.status), record.created_at, record.updated_at,
        workspace_path=record.workspace_path, last_error=record.last_error,
        current_snapshot_id=record.current_snapshot_id,
        owner_id=record.owner_id,
        archived_at=record.archived_at,
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
        record.execution_token, record.lease_expires_at,
        record.last_heartbeat_at, record.worker_id,
    )
