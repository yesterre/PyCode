from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from contextlib import AbstractContextManager
from typing import Protocol
from uuid import UUID

from backend.app.core.errors import BackendError
from backend.app.config import ExecutionLeaseSettings, QueuedReconciliationSettings
from backend.app.core.models import (
    BackgroundTask, BackgroundTaskClaimStatus, Project,
)
from backend.app.repositories import UnitOfWork
from backend.app.services.idempotency import repository_index_fingerprint


logger = logging.getLogger("pycode.backend.tasks")
HEALTH_TASK_TYPE = "health"
HEALTH_CELERY_TASK_NAME = "pycode.tasks.health"
REPOSITORY_INDEX_TASK_TYPE = "repository_index"
REPOSITORY_INDEX_CELERY_TASK_NAME = "pycode.tasks.index_repository"
AGENT_RUN_TASK_TYPE = "agent_run"
AGENT_RUN_CELERY_TASK_NAME = "pycode.tasks.run_agent"


class BackgroundTaskDispatcher(Protocol):
    def enqueue_health(self, task_id: UUID) -> None: ...

    def enqueue_repository_index(self, task_id: UUID) -> None: ...

    def enqueue_agent_run(self, task_id: UUID) -> None: ...


class BackgroundTaskService:
    """Own persistent task lifecycle rules independently of HTTP and Celery."""

    def __init__(
        self, unit_of_work: UnitOfWork,
        dispatcher: BackgroundTaskDispatcher | None = None,
        reconciliation_settings: QueuedReconciliationSettings | None = None,
        lease_settings: ExecutionLeaseSettings | None = None,
        heartbeat_factory: Callable[
            [BackgroundTask], AbstractContextManager
        ] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.unit_of_work = unit_of_work
        self.dispatcher = dispatcher
        self.reconciliation_settings = (
            reconciliation_settings or QueuedReconciliationSettings()
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lease_settings = lease_settings or ExecutionLeaseSettings()
        self.heartbeat_factory = heartbeat_factory

    def create_health_task(self) -> BackgroundTask:
        if self.dispatcher is None:
            raise RuntimeError("A task dispatcher is required to enqueue a background task.")
        with self.unit_of_work.transaction():
            task = self.unit_of_work.background_tasks.create(
                HEALTH_TASK_TYPE,
                next_enqueue_attempt_at=self.initial_enqueue_due_at(),
            )
        return self._enqueue(task, self.dispatcher.enqueue_health)

    def create_repository_index_task(
        self, project_id: UUID, idempotency_key: str | None = None,
    ) -> BackgroundTask:
        """Trusted internal submission; HTTP uses the owned entry point."""
        return self._create_repository_index_task(project_id, idempotency_key)

    def create_owned_repository_index_task(
        self, project_id: UUID, owner_id: UUID, idempotency_key: str | None = None,
    ) -> BackgroundTask:
        if owner_id is None:
            raise BackendError("project_not_found", "Project does not exist.")
        return self._create_repository_index_task(project_id, idempotency_key, owner_id=owner_id)

    def _create_repository_index_task(
        self, project_id: UUID, idempotency_key: str | None, *, owner_id: UUID | None = None,
    ) -> BackgroundTask:
        if self.dispatcher is None:
            raise RuntimeError("A task dispatcher is required to enqueue a background task.")
        fingerprint = (
            repository_index_fingerprint(project_id)
            if idempotency_key is not None else None
        )
        with self.unit_of_work.transaction():
            if owner_id is None:
                self.unit_of_work.projects.get(project_id)
            else:
                self.unit_of_work.projects.get_owned(project_id, owner_id)
            creation = self.unit_of_work.background_tasks.create_submission(
                REPOSITORY_INDEX_TASK_TYPE,
                project_id=project_id,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                next_enqueue_attempt_at=self.initial_enqueue_due_at(),
            )
        if creation.created:
            return self._enqueue(
                creation.task, self.dispatcher.enqueue_repository_index,
            )
        return creation.task

    def get(self, task_id: UUID) -> BackgroundTask:
        with self.unit_of_work.transaction():
            return self.unit_of_work.background_tasks.get(task_id)

    def enqueue_agent_run(self, task: BackgroundTask) -> BackgroundTask:
        if self.dispatcher is None:
            raise RuntimeError("A task dispatcher is required to enqueue a background task.")
        if task.task_type != AGENT_RUN_TASK_TYPE or task.agent_run_id is None:
            raise BackendError(
                "background_task_state_conflict",
                "Background task cannot make the requested state transition.",
            )
        return self._enqueue(task, self.dispatcher.enqueue_agent_run)

    def initial_enqueue_due_at(self) -> datetime:
        return self.clock() + timedelta(
            seconds=self.reconciliation_settings.publish_grace_seconds,
        )

    def execute_health(
        self, task_id: UUID, operation: Callable[[], None] | None = None, *,
        worker_id: str | None = None,
    ) -> BackgroundTask:
        return self._execute(
            task_id, HEALTH_TASK_TYPE,
            lambda _, _ownership: (operation or _health_operation)(),
            error_mapper=_health_task_error,
            worker_id=worker_id,
        )

    def execute_repository_index(
        self, task_id: UUID, index_project: Callable[[UUID], Project], *,
        owned_operation: Callable[[BackgroundTask, object], Project] | None = None,
        worker_id: str | None = None,
    ) -> BackgroundTask:
        def operation(task: BackgroundTask, ownership) -> UUID:
            if task.project_id is None:  # pragma: no cover - persistent invariant
                raise BackendError(
                    "background_task_state_conflict",
                    "Background task cannot make the requested state transition.",
                )
            project = (
                owned_operation(task, ownership)
                if owned_operation is not None
                else index_project(task.project_id)
            )
            if project.current_snapshot_id is None:  # pragma: no cover - Phase 4 invariant
                raise BackendError(
                    "database_conflict", "A related persistent record is invalid.",
                )
            return project.current_snapshot_id

        return self._execute(
            task_id, REPOSITORY_INDEX_TASK_TYPE, operation,
            error_mapper=_repository_index_task_error,
            worker_id=worker_id,
        )

    def _execute(
        self, task_id: UUID, expected_type: str,
        operation: Callable[[BackgroundTask, object], UUID | None], *,
        error_mapper: Callable[[Exception], tuple[str, str]],
        worker_id: str | None = None,
    ) -> BackgroundTask:
        with self.unit_of_work.transaction():
            claim = self.unit_of_work.background_tasks.claim(
                task_id,
                lease_ttl_seconds=self.lease_settings.ttl_seconds,
                worker_id=worker_id,
            )
            if claim.status == BackgroundTaskClaimStatus.NOT_FOUND:
                raise BackendError(
                    "background_task_not_found", "Background task does not exist.",
                )
            task = claim.task
            if task is None:  # pragma: no cover - repository contract
                raise RuntimeError("Background task claim returned no task.")

        if task.task_type != expected_type:
            error = BackendError(
                "background_task_state_conflict",
                "Background task cannot make the requested state transition.",
            )
            if claim.status == BackgroundTaskClaimStatus.CLAIMED:
                with self.unit_of_work.transaction():
                    self.unit_of_work.background_tasks.mark_failed(
                        task_id,
                        error_code=error.code,
                        error_message=error.message,
                        execution_token=task.execution_token,
                    )
            raise error
        if claim.status != BackgroundTaskClaimStatus.CLAIMED:
            logger.info(
                "Duplicate background task delivery ignored task_id=%s "
                "task_type=%s claim_status=%s",
                task_id, expected_type, claim.status,
            )
            return task

        logger.info(
            "Background task execution claimed task_id=%s task_type=%s "
            "worker_id=%s execution_token=%s lease_expires_at=%s attempt_count=%s",
            task.id, task.task_type, task.worker_id,
            str(task.execution_token)[:8], task.lease_expires_at,
            task.attempt_count,
        )

        execution_token = task.execution_token
        if execution_token is None:  # pragma: no cover - owner claim invariant
            raise RuntimeError("Claimed background task has no execution token.")

        with self._heartbeat(task) as ownership:
            try:
                ownership.ensure_owned()
                snapshot_id = operation(task, ownership)
                ownership.ensure_owned()
            except Exception as exc:
                error_code, error_message = error_mapper(exc)
                logger.error(
                    "Background task execution failed task_id=%s task_type=%s error_code=%s",
                    task_id, expected_type, error_code,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                if error_code == "database_unavailable":
                    # Ownership is unknown during an infrastructure outage.
                    # Leave the durable running lease for stale reconciliation
                    # and let the Celery adapter reject/requeue the delivery.
                    raise
                with self.unit_of_work.transaction():
                    failed = self.unit_of_work.background_tasks.mark_failed(
                        task_id, error_code=error_code, error_message=error_message,
                        execution_token=execution_token,
                    )
                if failed is None:
                    logger.warning(
                        "Stale task failure write rejected task_id=%s task_type=%s "
                        "execution_token=%s",
                        task_id, expected_type, str(execution_token)[:8],
                    )
                    return self.get(task_id)
                raise

            with self.unit_of_work.transaction():
                completed = self.unit_of_work.background_tasks.mark_completed(
                    task_id, snapshot_id=snapshot_id,
                    execution_token=execution_token,
                )
            if completed is None:
                logger.warning(
                    "Stale task completion write rejected task_id=%s task_type=%s "
                    "execution_token=%s",
                    task_id, expected_type, str(execution_token)[:8],
                )
                return self.get(task_id)
            return completed

    def _heartbeat(self, task: BackgroundTask) -> AbstractContextManager:
        if self.heartbeat_factory is not None:
            return self.heartbeat_factory(task)
        from backend.app.infrastructure.execution import InlineExecutionOwnership

        def check(task_id: UUID, token: UUID) -> bool:
            with self.unit_of_work.transaction():
                return self.unit_of_work.background_tasks.guard_execution(
                    task_id, token,
                ) is not None

        return InlineExecutionOwnership(task, check)

    def _enqueue(
        self, task: BackgroundTask, enqueue: Callable[[UUID], None],
    ) -> BackgroundTask:
        attempted_at = self.clock()
        with self.unit_of_work.transaction():
            reserved = self.unit_of_work.background_tasks.reserve_enqueue_attempt(
                task.id,
                attempted_at,
                lambda attempt: attempted_at + timedelta(
                    seconds=enqueue_backoff_seconds(
                        attempt,
                        self.reconciliation_settings.max_backoff_seconds,
                    ),
                ),
            )
        logger.info(
            "Background task publish reserved task_id=%s task_type=%s "
            "enqueue_attempt_count=%s next_enqueue_attempt_at=%s",
            task.id, task.task_type, reserved.enqueue_attempt_count,
            reserved.next_enqueue_attempt_at,
        )
        try:
            enqueue(task.id)
        except Exception as exc:
            logger.error(
                "Background task enqueue failed task_id=%s task_type=%s "
                "enqueue_attempt_count=%s next_enqueue_attempt_at=%s error_type=%s",
                task.id, task.task_type, reserved.enqueue_attempt_count,
                reserved.next_enqueue_attempt_at, type(exc).__name__,
            )
            raise BackendError(
                "task_queue_unavailable", "The background task queue is unavailable.",
            ) from exc
        with self.unit_of_work.transaction():
            confirmed = self.unit_of_work.background_tasks.mark_enqueue_confirmed(
                task.id, self.clock(),
            )
        logger.info(
            "Background task publish confirmed task_id=%s task_type=%s "
            "enqueue_attempt_count=%s",
            task.id, task.task_type, confirmed.enqueue_attempt_count,
        )
        return confirmed


def _health_operation() -> None:
    """Small deterministic operation proving that a worker executed the task."""


def _health_task_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, BackendError):
        return exc.code, exc.message
    return "background_task_failed", "Background task execution failed."


def _repository_index_task_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, BackendError):
        return exc.code, exc.message
    if isinstance(exc, PermissionError):
        return "permission_denied", "Project file access was denied."
    if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        return "path_not_found", "Repository or target path does not exist."
    if isinstance(exc, (SyntaxError, UnicodeError)):
        return "invalid_source", "Python source could not be parsed or decoded."
    return "internal_error", "An unexpected server error occurred."


def enqueue_backoff_seconds(attempt_count: int, maximum: float) -> float:
    schedule = (5.0, 15.0, 30.0, 60.0)
    if attempt_count <= len(schedule):
        delay = schedule[max(attempt_count, 1) - 1]
    else:
        delay = schedule[-1] * (2 ** (attempt_count - len(schedule)))
    return min(delay, maximum)
