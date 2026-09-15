from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import UUID

from backend.app.core.errors import BackendError
from backend.app.config import QueuedReconciliationSettings
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
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.unit_of_work = unit_of_work
        self.dispatcher = dispatcher
        self.reconciliation_settings = (
            reconciliation_settings or QueuedReconciliationSettings()
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))

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
        if self.dispatcher is None:
            raise RuntimeError("A task dispatcher is required to enqueue a background task.")
        fingerprint = (
            repository_index_fingerprint(project_id)
            if idempotency_key is not None else None
        )
        with self.unit_of_work.transaction():
            self.unit_of_work.projects.get(project_id)
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
        self, task_id: UUID, operation: Callable[[], None] | None = None,
    ) -> BackgroundTask:
        return self._execute(
            task_id, HEALTH_TASK_TYPE,
            lambda _: (operation or _health_operation)(),
            error_mapper=_health_task_error,
        )

    def execute_repository_index(
        self, task_id: UUID, index_project: Callable[[UUID], Project],
    ) -> BackgroundTask:
        def operation(task: BackgroundTask) -> UUID:
            if task.project_id is None:  # pragma: no cover - persistent invariant
                raise BackendError(
                    "background_task_state_conflict",
                    "Background task cannot make the requested state transition.",
                )
            project = index_project(task.project_id)
            if project.current_snapshot_id is None:  # pragma: no cover - Phase 4 invariant
                raise BackendError(
                    "database_conflict", "A related persistent record is invalid.",
                )
            return project.current_snapshot_id

        return self._execute(
            task_id, REPOSITORY_INDEX_TASK_TYPE, operation,
            error_mapper=_repository_index_task_error,
        )

    def _execute(
        self, task_id: UUID, expected_type: str,
        operation: Callable[[BackgroundTask], UUID | None], *,
        error_mapper: Callable[[Exception], tuple[str, str]],
    ) -> BackgroundTask:
        with self.unit_of_work.transaction():
            claim = self.unit_of_work.background_tasks.claim(task_id)
            if claim.status == BackgroundTaskClaimStatus.NOT_FOUND:
                raise BackendError(
                    "background_task_not_found", "Background task does not exist.",
                )
            task = claim.task
            if task is None:  # pragma: no cover - repository contract
                raise RuntimeError("Background task claim returned no task.")
            if task.task_type != expected_type:
                raise BackendError(
                    "background_task_state_conflict",
                    "Background task cannot make the requested state transition.",
                )
            if claim.status != BackgroundTaskClaimStatus.CLAIMED:
                logger.info(
                    "Duplicate background task delivery ignored task_id=%s "
                    "task_type=%s claim_status=%s",
                    task_id, expected_type, claim.status,
                )
                return task

        try:
            snapshot_id = operation(task)
        except Exception as exc:
            error_code, error_message = error_mapper(exc)
            logger.error(
                "Background task execution failed task_id=%s task_type=%s error_code=%s",
                task_id, expected_type, error_code,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            with self.unit_of_work.transaction():
                self.unit_of_work.background_tasks.mark_failed(
                    task_id, error_code=error_code, error_message=error_message,
                )
            raise

        with self.unit_of_work.transaction():
            return self.unit_of_work.background_tasks.mark_completed(
                task_id, snapshot_id=snapshot_id,
            )

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
