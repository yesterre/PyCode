from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from backend.app.core.errors import BackendError
from backend.app.core.models import BackgroundTask, Project
from backend.app.repositories import UnitOfWork


logger = logging.getLogger("pycode.backend.tasks")
HEALTH_TASK_TYPE = "health"
HEALTH_CELERY_TASK_NAME = "pycode.tasks.health"
REPOSITORY_INDEX_TASK_TYPE = "repository_index"
REPOSITORY_INDEX_CELERY_TASK_NAME = "pycode.tasks.index_repository"


class BackgroundTaskDispatcher(Protocol):
    def enqueue_health(self, task_id: UUID) -> None: ...

    def enqueue_repository_index(self, task_id: UUID) -> None: ...


class BackgroundTaskService:
    """Own persistent task lifecycle rules independently of HTTP and Celery."""

    def __init__(
        self, unit_of_work: UnitOfWork,
        dispatcher: BackgroundTaskDispatcher | None = None,
    ) -> None:
        self.unit_of_work = unit_of_work
        self.dispatcher = dispatcher

    def create_health_task(self) -> BackgroundTask:
        if self.dispatcher is None:
            raise RuntimeError("A task dispatcher is required to enqueue a background task.")
        with self.unit_of_work.transaction():
            task = self.unit_of_work.background_tasks.create(HEALTH_TASK_TYPE)
        self._enqueue(task, self.dispatcher.enqueue_health)
        return task

    def create_repository_index_task(self, project_id: UUID) -> BackgroundTask:
        if self.dispatcher is None:
            raise RuntimeError("A task dispatcher is required to enqueue a background task.")
        with self.unit_of_work.transaction():
            self.unit_of_work.projects.get(project_id)
            task = self.unit_of_work.background_tasks.create(
                REPOSITORY_INDEX_TASK_TYPE, project_id=project_id,
            )
        self._enqueue(task, self.dispatcher.enqueue_repository_index)
        return task

    def get(self, task_id: UUID) -> BackgroundTask:
        with self.unit_of_work.transaction():
            return self.unit_of_work.background_tasks.get(task_id)

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
            task = self.unit_of_work.background_tasks.get(task_id)
            if task.task_type != expected_type:
                raise BackendError(
                    "background_task_state_conflict",
                    "Background task cannot make the requested state transition.",
                )
            task = self.unit_of_work.background_tasks.mark_running(task_id)

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

    @staticmethod
    def _enqueue(
        task: BackgroundTask, enqueue: Callable[[UUID], None],
    ) -> None:
        try:
            enqueue(task.id)
        except Exception as exc:
            logger.error(
                "Background task enqueue failed task_id=%s task_type=%s error_type=%s",
                task.id, task.task_type, type(exc).__name__,
            )
            raise BackendError(
                "task_queue_unavailable", "The background task queue is unavailable.",
            ) from exc


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
