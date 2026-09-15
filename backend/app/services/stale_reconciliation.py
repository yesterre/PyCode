from __future__ import annotations

import logging
from dataclasses import dataclass

from backend.app.config import StaleReconciliationSettings
from backend.app.core.errors import BackendError
from backend.app.core.models import AgentRunStatus, BackgroundTask, ProjectStatus
from backend.app.infrastructure.projects import ProjectOperationLocks
from backend.app.repositories import UnitOfWork
from backend.app.services.background_tasks import (
    AGENT_RUN_TASK_TYPE, REPOSITORY_INDEX_TASK_TYPE,
)


logger = logging.getLogger("pycode.backend.tasks")
RECONCILE_STALE_RUNNING_CELERY_TASK_NAME = "pycode.tasks.reconcile_stale_running"
WORKER_LOST_MESSAGE = "Worker execution ownership was lost before completion."
WORKER_LOST_AFTER_CORE_MESSAGE = (
    "Worker execution was lost after the Agent Core boundary and execution "
    "was not retried."
)


@dataclass(frozen=True)
class StaleReconciliationResult:
    scanned: int = 0
    requeued: int = 0
    completed: int = 0
    failed: int = 0
    deferred: int = 0


class StaleRunningReconciler:
    """Make conservative, token-aware decisions for expired running tasks."""

    def __init__(
        self,
        unit_of_work: UnitOfWork,
        project_operations: ProjectOperationLocks,
        settings: StaleReconciliationSettings | None = None,
    ) -> None:
        self.uow = unit_of_work
        self.project_operations = project_operations
        self.settings = settings or StaleReconciliationSettings()

    def reconcile(self) -> StaleReconciliationResult:
        with self.uow.transaction():
            candidates = self.uow.background_tasks.list_stale_running(
                self.settings.batch_size,
            )
        result = StaleReconciliationResult(scanned=len(candidates))
        for candidate in candidates:
            if candidate.project_id is None:
                decision = self._decide(candidate)
            else:
                try:
                    with self.project_operations.operation(candidate.project_id):
                        decision = self._decide(candidate)
                except BackendError as exc:
                    if exc.code in {"project_busy", "project_lock_unavailable"}:
                        logger.info(
                            "Stale recovery deferred by project lock task_id=%s "
                            "task_type=%s",
                            candidate.id, candidate.task_type,
                        )
                        decision = "deferred"
                    else:
                        raise
            logger.info(
                "Stale recovery decision task_id=%s task_type=%s worker_id=%s "
                "execution_token=%s decision=%s",
                candidate.id, candidate.task_type, candidate.worker_id,
                str(candidate.execution_token)[:8], decision,
            )
            result = StaleReconciliationResult(
                scanned=result.scanned,
                requeued=result.requeued + (decision == "requeued"),
                completed=result.completed + (decision == "completed"),
                failed=result.failed + (decision == "failed"),
                deferred=result.deferred + (decision in {"deferred", "noop"}),
            )
        return result

    def _decide(self, candidate: BackgroundTask) -> str:
        logger.warning(
            "Stale execution detected task_id=%s task_type=%s worker_id=%s "
            "execution_token=%s lease_expires_at=%s",
            candidate.id, candidate.task_type, candidate.worker_id,
            str(candidate.execution_token)[:8], candidate.lease_expires_at,
        )
        if candidate.task_type == REPOSITORY_INDEX_TASK_TYPE:
            return self._repository(candidate)
        if candidate.task_type == AGENT_RUN_TASK_TYPE:
            return self._agent(candidate)
        with self.uow.transaction():
            failed = self.uow.background_tasks.fail_if_stale(
                candidate.id, candidate.execution_token,
                error_code="worker_execution_lost",
                error_message=WORKER_LOST_MESSAGE,
            )
        return "failed" if failed is not None else "noop"

    def _repository(self, candidate: BackgroundTask) -> str:
        with self.uow.transaction():
            current = self.uow.background_tasks.get(candidate.id)
            if current.snapshot_id is None or current.execution_token is None:
                failed = self.uow.background_tasks.fail_if_stale(
                    current.id, current.execution_token,
                    error_code="worker_execution_lost",
                    error_message=WORKER_LOST_MESSAGE,
                )
                return "failed" if failed is not None else "noop"
            # Establish the same BackgroundTask-first lock order used by worker
            # correctness boundaries. This fences heartbeat/activation races.
            current = self.uow.background_tasks.guard_execution(
                current.id, current.execution_token,
            )
            if current is None:
                return "noop"
            try:
                snapshot = self.uow.snapshots.get(current.snapshot_id)
                project = self.uow.projects.get(current.project_id)
            except BackendError:
                failed = self.uow.background_tasks.fail_if_stale(
                    current.id, current.execution_token,
                    error_code="worker_execution_lost",
                    error_message=WORKER_LOST_MESSAGE,
                )
                return "failed" if failed is not None else "noop"
            if snapshot.project_id != current.project_id:
                return self._fail_repository_consistency(current)
            if (
                snapshot.status == "ready"
                and project.status == ProjectStatus.READY
                and project.current_snapshot_id == snapshot.id
            ):
                completed = self.uow.background_tasks.complete_if_stale(
                    current.id, current.execution_token,
                )
                return "completed" if completed is not None else "noop"
            if (
                snapshot.status != "indexing"
                or project.current_snapshot_id not in {None, snapshot.id}
                or current.attempt_count >= self.settings.max_execution_attempts
            ):
                return self._fail_repository_consistency(current)
            requeued = self.uow.background_tasks.requeue_if_stale(
                current.id, current.execution_token,
            )
            return "requeued" if requeued is not None else "noop"

    def _fail_repository_consistency(self, task: BackgroundTask) -> str:
        failed = self.uow.background_tasks.fail_if_stale(
            task.id, task.execution_token,
            error_code="worker_execution_lost",
            error_message=WORKER_LOST_MESSAGE,
        )
        return "failed" if failed is not None else "noop"

    def _agent(self, candidate: BackgroundTask) -> str:
        with self.uow.transaction():
            current = self.uow.background_tasks.get(candidate.id)
            if current.execution_token is None:
                failed = self.uow.background_tasks.fail_if_stale(
                    current.id, None,
                    error_code="worker_execution_lost",
                    error_message=WORKER_LOST_MESSAGE,
                )
                return "failed" if failed is not None else "noop"
            # Agent Core marker transactions lock the BackgroundTask first.
            # Taking the same row first prevents a marker/requeue split-brain.
            current = self.uow.background_tasks.guard_execution(
                current.id, current.execution_token,
            )
            if current is None:
                return "noop"
            run = None
            snapshot = None
            project = None
            if current.agent_run_id is not None:
                try:
                    run = self.uow.agent_runs.get(current.agent_run_id)
                except BackendError:
                    pass
            if current.snapshot_id is not None:
                try:
                    snapshot = self.uow.snapshots.get(current.snapshot_id)
                except BackendError:
                    pass
            if current.project_id is not None:
                try:
                    project = self.uow.projects.get(current.project_id)
                except BackendError:
                    pass
            relation_valid = (
                run is not None
                and snapshot is not None
                and project is not None
                and current.project_id is not None
                and run.status == AgentRunStatus.RUNNING
                and run.project_id == current.project_id
                and run.snapshot_id == current.snapshot_id
                and snapshot.project_id == current.project_id
                and snapshot.status == "ready"
                and project.status == ProjectStatus.READY
                and project.current_snapshot_id == snapshot.id
            )
            traces = (
                self.uow.trace_events.list_for_run(run.id) if run is not None else []
            )
            can_requeue = (
                relation_valid
                and current.execution_token is not None
                and run is not None
                and run.core_started_at is None
                and run.result is None
                and run.finished_at is None
                and not traces
                and current.attempt_count < self.settings.max_execution_attempts
            )
            if can_requeue:
                requeued = self.uow.background_tasks.requeue_if_stale(
                    current.id, current.execution_token,
                )
                if requeued is None:
                    return "noop"
                self.uow.agent_runs.requeue_running(run.id)
                return "requeued"

            post_core = run is not None and run.core_started_at is not None
            error_code = (
                "worker_lost_after_agent_core"
                if post_core else "worker_execution_lost"
            )
            error_message = (
                WORKER_LOST_AFTER_CORE_MESSAGE if post_core else WORKER_LOST_MESSAGE
            )
            if post_core:
                logger.warning(
                    "Agent stale recovery selected post-core no-retry "
                    "task_id=%s run_id=%s execution_token=%s",
                    current.id, run.id, str(current.execution_token)[:8],
                )
            failed = self.uow.background_tasks.fail_if_stale(
                current.id, current.execution_token,
                error_code=error_code,
                error_message=error_message,
            )
            if failed is None:
                return "noop"
            if run is not None:
                if run.status == AgentRunStatus.QUEUED:
                    run = self.uow.agent_runs.mark_running(run.id)
                if run.status == AgentRunStatus.RUNNING:
                    self.uow.agent_runs.fail(run.id, error_message)
            return "failed"
