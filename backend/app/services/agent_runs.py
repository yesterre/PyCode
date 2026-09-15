from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from backend.app.core.errors import BackendError
from backend.app.core.models import (
    AgentCoreStartStatus, AgentRun, AgentRunStatus, BackgroundTask,
    BackgroundTaskClaimStatus, BackgroundTaskStatus,
)
from backend.app.infrastructure.agent_workspace import AgentSnapshotWorkspace
from backend.app.infrastructure.artifacts import SnapshotArtifactBundle
from backend.app.integrations.agent_trace import map_agent_trace
from backend.app.integrations.pycode import PyCodeAdapter
from backend.app.repositories import UnitOfWork
from backend.app.services.background_tasks import (
    AGENT_RUN_TASK_TYPE,
    BackgroundTaskService,
)
from backend.app.services.idempotency import agent_run_fingerprint
from backend.app.services.projects import ProjectService


logger = logging.getLogger("pycode.backend.tasks")


@dataclass(frozen=True)
class AgentRunSubmission:
    run: AgentRun
    background_task: BackgroundTask


class AgentRunService:
    """Own the asynchronous AgentRun boundary outside the protected Agent Core."""

    def __init__(
        self,
        unit_of_work: UnitOfWork,
        background_tasks: BackgroundTaskService,
        projects: ProjectService,
        adapter: PyCodeAdapter,
        agent_workspace: AgentSnapshotWorkspace,
    ) -> None:
        self.unit_of_work = unit_of_work
        self.background_tasks = background_tasks
        self.projects = projects
        self.adapter = adapter
        self.agent_workspace = agent_workspace

    def submit(
        self, project_id: UUID, task_text: str,
        idempotency_key: str | None = None,
    ) -> AgentRunSubmission:
        fingerprint = (
            agent_run_fingerprint(project_id, task_text)
            if idempotency_key is not None else None
        )
        with self.unit_of_work.transaction():
            project = self.unit_of_work.projects.get(project_id)
            creation = self.unit_of_work.background_tasks.create_submission(
                AGENT_RUN_TASK_TYPE,
                project_id=project_id,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                next_enqueue_attempt_at=self.background_tasks.initial_enqueue_due_at(),
            )
            if not creation.created:
                background_task = creation.task
                if (
                    background_task.agent_run_id is None
                    or background_task.snapshot_id is None
                ):
                    raise self._consistency_error()
                try:
                    run = self.unit_of_work.agent_runs.get(
                        background_task.agent_run_id,
                    )
                except BackendError as exc:
                    if exc.code == "agent_run_not_found":
                        raise self._consistency_error() from exc
                    raise
                if (
                    run.project_id != project_id
                    or run.snapshot_id != background_task.snapshot_id
                ):
                    raise self._consistency_error()
                return AgentRunSubmission(run, background_task)
            if project.current_snapshot_id is None:
                raise BackendError(
                    "project_not_ready", "Build the project index before analysis.",
                )
            snapshot = self.unit_of_work.snapshots.get(project.current_snapshot_id)
            if snapshot.project_id != project_id or snapshot.status != "ready":
                raise BackendError(
                    "project_not_ready", "Build the project index before analysis.",
                )
            run = self.unit_of_work.agent_runs.create_queued(
                project_id,
                snapshot.id,
                task=task_text,
                run_type="agent",
                model=None,
            )
            background_task = self.unit_of_work.background_tasks.link_agent_run(
                creation.task.id, run.id, snapshot.id,
            )
        background_task = self.background_tasks.enqueue_agent_run(background_task)
        return AgentRunSubmission(run, background_task)

    def get(self, run_id: UUID) -> AgentRun:
        with self.unit_of_work.transaction():
            return self.unit_of_work.agent_runs.get(run_id)

    def execute(self, task_id: UUID) -> AgentRun:
        with self.unit_of_work.transaction():
            claim = self.unit_of_work.background_tasks.claim(task_id)
            if claim.status == BackgroundTaskClaimStatus.NOT_FOUND:
                raise BackendError(
                    "background_task_not_found", "Background task does not exist.",
                )
            background_task = claim.task
            if background_task is None:  # pragma: no cover - repository contract
                raise RuntimeError("Background task claim returned no task.")
            if (
                background_task.task_type != AGENT_RUN_TASK_TYPE
                or background_task.agent_run_id is None
                or background_task.project_id is None
                or background_task.snapshot_id is None
            ):
                raise self._consistency_error()
            try:
                run = self.unit_of_work.agent_runs.get(background_task.agent_run_id)
            except BackendError as exc:
                if exc.code == "agent_run_not_found":
                    raise self._consistency_error() from exc
                raise
            if (
                run.project_id != background_task.project_id
                or run.snapshot_id != background_task.snapshot_id
            ):
                raise self._consistency_error()

            if claim.status != BackgroundTaskClaimStatus.CLAIMED:
                expected_run_status = {
                    BackgroundTaskStatus.RUNNING: AgentRunStatus.RUNNING,
                    BackgroundTaskStatus.COMPLETED: AgentRunStatus.COMPLETED,
                    BackgroundTaskStatus.FAILED: AgentRunStatus.FAILED,
                }.get(background_task.status)
                if expected_run_status is None or run.status != expected_run_status:
                    raise self._consistency_error()
                logger.info(
                    "Duplicate agent task delivery ignored task_id=%s run_id=%s "
                    "claim_status=%s",
                    task_id, run.id, claim.status,
                )
                return run

            if run.status != AgentRunStatus.QUEUED:
                raise self._consistency_error()
            run = self.unit_of_work.agent_runs.mark_running(run.id)

        try:
            result = self.projects.execute_on_snapshot(
                run.project_id,
                background_task.snapshot_id,
                lambda root, _snapshot, bundle: self._run_agent(
                    root, bundle, run.task, task_id=task_id, run_id=run.id,
                ),
            )
        except Exception as exc:
            error_code, error_message = _agent_run_error(exc)
            logger.error(
                "Agent background task failed task_id=%s run_id=%s error_code=%s",
                task_id,
                run.id,
                error_code,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            self._fail(task_id, run.id, error_code, error_message)
            raise

        trace_events = map_agent_trace(result.trace)
        if not result.ok:
            error_code = "agent_run_failed"
            error_message = "Agent execution did not complete successfully."
            self._fail(
                task_id,
                run.id,
                error_code,
                error_message,
                trace_events=trace_events,
            )
            raise BackendError(error_code, error_message)

        with self.unit_of_work.transaction():
            self._persist_trace(run.id, trace_events)
            completed = self.unit_of_work.agent_runs.complete(run.id, result.answer)
            self.unit_of_work.background_tasks.mark_completed(task_id)
        return completed

    def _run_agent(
        self,
        repository,
        bundle: SnapshotArtifactBundle,
        task_text: str,
        *,
        task_id: UUID,
        run_id: UUID,
    ) -> Any:
        self.agent_workspace.prepare(repository, bundle)
        with self.unit_of_work.transaction():
            background_task = self.unit_of_work.background_tasks.get(task_id)
            current_run = self.unit_of_work.agent_runs.get(run_id)
            if (
                background_task.status != BackgroundTaskStatus.RUNNING
                or background_task.agent_run_id != run_id
                or current_run.status != AgentRunStatus.RUNNING
                or current_run.project_id != background_task.project_id
                or current_run.snapshot_id != background_task.snapshot_id
            ):
                raise self._consistency_error()
            marked = self.unit_of_work.agent_runs.mark_core_started(run_id)
            if marked.status != AgentCoreStartStatus.MARKED or marked.run is None:
                # ALREADY_MARKED is a historical no-retry boundary, not
                # permission to transparently enter Core again.
                raise self._consistency_error()
        logger.info(
            "Agent Core boundary persisted run_id=%s core_started_at=%s",
            run_id,
            marked.run.core_started_at,
        )
        return self.adapter.run_agent(repository, task_text)

    def _fail(
        self,
        task_id: UUID,
        run_id: UUID,
        error_code: str,
        error_message: str,
        *,
        trace_events=None,
    ) -> None:
        with self.unit_of_work.transaction():
            self._persist_trace(run_id, trace_events or [])
            self.unit_of_work.agent_runs.fail(run_id, error_message)
            self.unit_of_work.background_tasks.mark_failed(
                task_id,
                error_code=error_code,
                error_message=error_message,
            )

    def _persist_trace(self, run_id: UUID, events) -> None:
        for sequence, event_type, tool_name, payload in events:
            self.unit_of_work.trace_events.create(
                run_id,
                sequence,
                event_type,
                tool_name=tool_name,
                payload=payload,
            )

    @staticmethod
    def _consistency_error() -> BackendError:
        return BackendError(
            "background_task_consistency_error",
            "Background task and Agent run state are inconsistent.",
        )


def _agent_run_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, BackendError):
        return exc.code, exc.message
    if isinstance(exc, PermissionError):
        return "permission_denied", "Project file access was denied."
    if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        return "path_not_found", "Repository or target path does not exist."
    if isinstance(exc, (SyntaxError, UnicodeError)):
        return "invalid_source", "Python source could not be parsed or decoded."
    return "agent_run_failed", "Agent execution failed."
