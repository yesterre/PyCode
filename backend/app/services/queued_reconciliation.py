from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from backend.app.config import QueuedReconciliationSettings
from backend.app.core.models import BackgroundTask
from backend.app.repositories import UnitOfWork
from backend.app.services.background_tasks import (
    AGENT_RUN_TASK_TYPE,
    HEALTH_TASK_TYPE,
    REPOSITORY_INDEX_TASK_TYPE,
    BackgroundTaskDispatcher,
    enqueue_backoff_seconds,
)


logger = logging.getLogger("pycode.backend.tasks")
RECONCILE_QUEUED_CELERY_TASK_NAME = "pycode.tasks.reconcile_queued"


@dataclass(frozen=True)
class QueuedReconciliationResult:
    candidate_count: int
    confirmed_count: int
    failed_count: int
    unsupported_count: int


class QueuedTaskReconciler:
    """Republish reserved queued tasks without executing business operations."""

    def __init__(
        self,
        unit_of_work: UnitOfWork,
        dispatcher: BackgroundTaskDispatcher,
        settings: QueuedReconciliationSettings | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.unit_of_work = unit_of_work
        self.dispatcher = dispatcher
        self.settings = settings or QueuedReconciliationSettings()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run(self) -> QueuedReconciliationResult:
        reserved_at = self.clock()
        logger.info("Queued task reconciliation started")
        with self.unit_of_work.transaction():
            candidates = (
                self.unit_of_work.background_tasks.reserve_queued_enqueue_candidates(
                    reserved_at,
                    self.settings.batch_size,
                    lambda attempt: reserved_at + timedelta(
                        seconds=enqueue_backoff_seconds(
                            attempt, self.settings.max_backoff_seconds,
                        ),
                    ),
                )
            )
        logger.info(
            "Queued task reconciliation candidates candidate_count=%s",
            len(candidates),
        )
        if not candidates:
            logger.info(
                "Queued task reconciliation reservation skipped; "
                "candidates are already reserved or not eligible"
            )

        confirmed = failed = unsupported = 0
        for task in candidates:
            logger.info(
                "Queued task reconciliation reserved task_id=%s task_type=%s "
                "enqueue_attempt_count=%s next_enqueue_attempt_at=%s",
                task.id, task.task_type, task.enqueue_attempt_count,
                task.next_enqueue_attempt_at,
            )
            publisher = self._publisher(task)
            if publisher is None:
                unsupported += 1
                logger.error(
                    "Queued task reconciliation skipped unsupported task "
                    "task_id=%s task_type=%s enqueue_attempt_count=%s "
                    "next_enqueue_attempt_at=%s",
                    task.id, task.task_type, task.enqueue_attempt_count,
                    task.next_enqueue_attempt_at,
                )
                continue
            try:
                publisher(task.id)
            except Exception as exc:
                failed += 1
                logger.warning(
                    "Queued task reconciliation publish failed task_id=%s "
                    "task_type=%s enqueue_attempt_count=%s "
                    "next_enqueue_attempt_at=%s error_type=%s",
                    task.id, task.task_type, task.enqueue_attempt_count,
                    task.next_enqueue_attempt_at, type(exc).__name__,
                )
                continue
            with self.unit_of_work.transaction():
                self.unit_of_work.background_tasks.mark_enqueue_confirmed(
                    task.id, self.clock(),
                )
            confirmed += 1
            logger.info(
                "Queued task reconciliation publish confirmed task_id=%s "
                "task_type=%s enqueue_attempt_count=%s",
                task.id, task.task_type, task.enqueue_attempt_count,
            )

        result = QueuedReconciliationResult(
            len(candidates), confirmed, failed, unsupported,
        )
        logger.info(
            "Queued task reconciliation completed candidate_count=%s "
            "confirmed_count=%s failed_count=%s unsupported_count=%s",
            result.candidate_count, result.confirmed_count,
            result.failed_count, result.unsupported_count,
        )
        return result

    def _publisher(self, task: BackgroundTask):
        return {
            HEALTH_TASK_TYPE: self.dispatcher.enqueue_health,
            REPOSITORY_INDEX_TASK_TYPE: self.dispatcher.enqueue_repository_index,
            AGENT_RUN_TASK_TYPE: self.dispatcher.enqueue_agent_run,
        }.get(task.task_type)
