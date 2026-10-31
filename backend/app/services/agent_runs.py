from __future__ import annotations

import logging
from inspect import signature
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from backend.app.core.errors import BackendError
from backend.app.core.realtime_trace import RealtimeTraceSession
from backend.app.core.application_trace import (
    ApplicationTraceFailure,
    ApplicationTraceEvent,
    ApplicationTraceNormalization,
    FailureStage,
    PublicErrorCategory,
    application_event_from_record,
    normalize_error,
)
from backend.app.core.models import (
    AgentCoreStartStatus, AgentRun, AgentRunStatus, BackgroundTask,
    BackgroundTaskClaimStatus, BackgroundTaskStatus,
)
from backend.app.infrastructure.agent_workspace import AgentSnapshotWorkspace
from backend.app.infrastructure.artifacts import SnapshotArtifactBundle
from backend.app.integrations.agent_trace import ApplicationTraceNormalizer
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
        trace_publisher=None,
    ) -> None:
        self.unit_of_work = unit_of_work
        self.background_tasks = background_tasks
        self.projects = projects
        self.adapter = adapter
        self.agent_workspace = agent_workspace
        self.trace_normalizer = ApplicationTraceNormalizer()
        self.trace_publisher = trace_publisher

    def submit(
        self, project_id: UUID, task_text: str,
        idempotency_key: str | None = None, *, owner_id: UUID,
    ) -> AgentRunSubmission:
        fingerprint = (
            agent_run_fingerprint(project_id, task_text)
            if idempotency_key is not None else None
        )
        with self.unit_of_work.transaction():
            project = self.unit_of_work.projects.get_owned(project_id, owner_id, for_update=True)
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
            project.ensure_not_archived()
            if project.current_snapshot_id is None:
                raise BackendError(
                    "project_not_ready", "Build the project index before analysis.",
                )
            snapshot = self.unit_of_work.snapshots.get_for_project(project_id, project.current_snapshot_id)
            if snapshot.status != "ready":
                raise BackendError(
                    "project_not_ready", "Build the project index before analysis.",
                )
            if self.background_tasks.admission is not None:
                self.background_tasks.admission.admit_task(self.unit_of_work, owner_id, "agent")
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

    def get_owned(self, run_id: UUID, owner_id: UUID) -> AgentRun:
        with self.unit_of_work.transaction():
            return self.unit_of_work.agent_runs.get_owned(run_id, owner_id)

    def get_trace(self, run_id: UUID, *, owner_id: UUID) -> list[ApplicationTraceEvent]:
        with self.unit_of_work.transaction():
            run = self.unit_of_work.agent_runs.get_owned(run_id, owner_id)
            if run.status in {AgentRunStatus.QUEUED, AgentRunStatus.RUNNING}:
                return []
            events = []
            for record in self.unit_of_work.trace_events.list_for_run(run_id):
                event = application_event_from_record(record)
                if event is not None:
                    events.append(event)
            return events

    def execute(self, task_id: UUID, *, worker_id: str | None = None) -> AgentRun:
        with self.unit_of_work.transaction():
            claim = self.unit_of_work.background_tasks.claim(
                task_id,
                lease_ttl_seconds=self.background_tasks.lease_settings.ttl_seconds,
                worker_id=worker_id,
            )
            if claim.status == BackgroundTaskClaimStatus.NOT_FOUND:
                raise BackendError(
                    "background_task_not_found", "Background task does not exist.",
                )
            background_task = claim.task
            if background_task is None:  # pragma: no cover - repository contract
                raise RuntimeError("Background task claim returned no task.")

        try:
            return self._execute_claimed(task_id, claim.status, background_task)
        except BackendError as exc:
            if (
                claim.status == BackgroundTaskClaimStatus.CLAIMED
                and exc.code == "background_task_consistency_error"
            ):
                self._terminalize_invalid_claim(background_task, exc)
            raise

    def _execute_claimed(
        self,
        task_id: UUID,
        claim_status: BackgroundTaskClaimStatus,
        background_task: BackgroundTask,
    ) -> AgentRun:
        with self.unit_of_work.transaction():
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

            if claim_status != BackgroundTaskClaimStatus.CLAIMED:
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
                    task_id, run.id, claim_status,
                )
                return run

            if run.status != AgentRunStatus.QUEUED:
                raise self._consistency_error()
            run = self.unit_of_work.agent_runs.mark_running(run.id)

        logger.info(
            "Agent execution claimed task_id=%s run_id=%s worker_id=%s "
            "execution_token=%s lease_expires_at=%s attempt_count=%s",
            background_task.id, run.id, background_task.worker_id,
            str(background_task.execution_token)[:8],
            background_task.lease_expires_at, background_task.attempt_count,
        )

        execution_token = background_task.execution_token
        if execution_token is None:  # pragma: no cover - owner claim invariant
            raise RuntimeError("Claimed Agent task has no execution token.")

        with self.background_tasks._heartbeat(background_task) as ownership:
            trace_holder: list[RealtimeTraceSession] = []
            try:
                ownership.ensure_owned()
                result = self.projects.execute_on_snapshot(
                    run.project_id,
                    background_task.snapshot_id,
                    lambda root, _snapshot, bundle: self._run_agent(
                        root, bundle, run.task, task_id=task_id, run_id=run.id,
                        execution_token=execution_token, ownership=ownership,
                        trace_holder=trace_holder,
                    ),
                )
                ownership.ensure_owned()
            except Exception as exc:
                error_code, error_message = _agent_run_error(exc)
                logger.error(
                    "Agent background task failed task_id=%s run_id=%s error_code=%s",
                    task_id,
                    run.id,
                    error_code,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                if error_code == "database_unavailable":
                    # Do not invent a terminal outcome while DB ownership is
                    # unknown. Celery rejects the delivery; the lease scanner
                    # later applies the pre/post-Core recovery policy.
                    raise
                if not self._fail(
                    task_id, run.id, execution_token, error_code, error_message,
                    failure=_failure_for_exception(error_code, exc),
                    trace_session=trace_holder[0] if trace_holder else None,
                ):
                    return self.get(run.id)
                raise

            if not result.ok:
                error_code = "agent_run_failed"
                error_message = "Agent execution did not complete successfully."
                if not self._fail(
                    task_id,
                    run.id,
                    execution_token,
                    error_code,
                    error_message,
                    result=result,
                    failure=_failure_for_result(result),
                    trace_session=trace_holder[0] if trace_holder else None,
                ):
                    return self.get(run.id)
                raise BackendError(error_code, error_message)

            with self.unit_of_work.transaction():
                if self.unit_of_work.background_tasks.guard_execution(
                    task_id, execution_token,
                ) is None:
                    completed = None
                else:
                    completed = self.unit_of_work.agent_runs.complete(
                        run.id, result.answer,
                    )
                    trace_session = trace_holder[0] if trace_holder else None
                    if trace_session is not None and trace_session.observed:
                        try:
                            terminal_event = trace_session.append_terminal(completed, result=result)
                        except Exception:
                            logger.warning("Realtime Trace terminal projection degraded run_id=%s", run.id, exc_info=True)
                            terminal_event = None
                            normalized = self._normalize_trace(completed, result=None)
                            self._persist_trace(run.id, normalized.events)
                        else:
                            self._persist_trace(run.id, trace_session.events)
                    else:
                        terminal_event = None
                        normalized = self._normalize_trace(completed, result=result)
                        self._persist_trace(run.id, normalized.events)
                    task = self.unit_of_work.background_tasks.mark_completed(
                        task_id, execution_token=execution_token,
                    )
                    if task is None:  # pragma: no cover - row guard is held
                        raise self._consistency_error()
            if completed is None:
                self._log_stale_write(task_id, run.id, execution_token)
                return self.get(run.id)
            if terminal_event is not None:
                trace_session.publish(terminal_event)
            return completed

    def _terminalize_invalid_claim(
        self, background_task: BackgroundTask, error: BackendError,
    ) -> None:
        token = background_task.execution_token
        if token is None:  # pragma: no cover - claim invariant
            return
        with self.unit_of_work.transaction():
            if self.unit_of_work.background_tasks.guard_execution(
                background_task.id, token,
            ) is None:
                logger.warning(
                    "Stale invalid-claim terminal write rejected task_id=%s "
                    "execution_token=%s",
                    background_task.id, str(token)[:8],
                )
                return
            if background_task.agent_run_id is not None:
                try:
                    run = self.unit_of_work.agent_runs.get(
                        background_task.agent_run_id,
                    )
                except BackendError as exc:
                    if exc.code != "agent_run_not_found":
                        raise
                else:
                    # A corrupt Task link must not fail an unrelated Run.
                    if (run.project_id == background_task.project_id
                            and run.snapshot_id == background_task.snapshot_id):
                        if run.status == AgentRunStatus.QUEUED:
                            run = self.unit_of_work.agent_runs.mark_running(run.id)
                        if run.status == AgentRunStatus.RUNNING:
                            self.unit_of_work.agent_runs.fail(run.id, error.message)
            self.unit_of_work.background_tasks.mark_failed(
                background_task.id,
                error_code=error.code,
                error_message=error.message,
                execution_token=token,
            )

    def _run_agent(
        self,
        repository,
        bundle: SnapshotArtifactBundle,
        task_text: str,
        *,
        task_id: UUID,
        run_id: UUID,
        execution_token: UUID,
        ownership,
        trace_holder: list[RealtimeTraceSession],
    ) -> Any:
        self.agent_workspace.prepare(repository, bundle)
        ownership.ensure_owned()
        with self.unit_of_work.transaction():
            background_task = self.unit_of_work.background_tasks.guard_execution(
                task_id, execution_token,
            )
            current_run = self.unit_of_work.agent_runs.get(run_id)
            if (
                background_task is None
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
        try:
            trace_session = RealtimeTraceSession(
                marked.run, ownership, self.trace_publisher,
            )
            trace_holder.append(trace_session)
        except Exception:
            logger.warning("Realtime Trace activation degraded run_id=%s", run_id, exc_info=True)
            return self.adapter.run_agent(repository, task_text)
        run_agent = self.adapter.run_agent
        if "trace_observer" in signature(run_agent).parameters:
            return run_agent(repository, task_text, trace_observer=trace_session.observe)
        return run_agent(repository, task_text)

    def _fail(
        self,
        task_id: UUID,
        run_id: UUID,
        execution_token: UUID,
        error_code: str,
        error_message: str,
        *,
        result=None,
        failure: ApplicationTraceFailure | None = None,
        trace_session: RealtimeTraceSession | None = None,
    ) -> bool:
        with self.unit_of_work.transaction():
            if self.unit_of_work.background_tasks.guard_execution(
                task_id, execution_token,
            ) is None:
                self._log_stale_write(task_id, run_id, execution_token)
                return False
            failed_run = self.unit_of_work.agent_runs.fail(run_id, error_message)
            if trace_session is not None and trace_session.observed and result is not None:
                try:
                    terminal_event = trace_session.append_terminal(
                        failed_run, result=result, failure=failure,
                    )
                except Exception:
                    logger.warning("Realtime Trace terminal projection degraded run_id=%s", run_id, exc_info=True)
                    terminal_event = None
                    normalized = self._normalize_trace(
                        failed_run, result=None, failure=failure,
                    )
                    self._persist_trace(run_id, normalized.events)
                else:
                    self._persist_trace(run_id, trace_session.events)
            else:
                terminal_event = None
                normalized = self._normalize_trace(
                    failed_run, result=result, failure=failure,
                )
                self._persist_trace(run_id, normalized.events)
            failed = self.unit_of_work.background_tasks.mark_failed(
                task_id,
                error_code=error_code,
                error_message=error_message,
                execution_token=execution_token,
            )
            if failed is None:  # pragma: no cover - row guard is held
                raise self._consistency_error()
        if terminal_event is not None:
            trace_session.publish(terminal_event)
        return True

    @staticmethod
    def _log_stale_write(task_id: UUID, run_id: UUID, token: UUID) -> None:
        logger.warning(
            "Stale Agent terminal write rejected task_id=%s run_id=%s "
            "execution_token=%s",
            task_id, run_id, str(token)[:8],
        )

    def _normalize_trace(self, run, *, result=None, failure=None):
        try:
            return self.trace_normalizer.normalize(
                run, result=result, failure=failure,
            )
        except Exception:
            logger.exception(
                "Application Trace normalization degraded run_id=%s", run.id,
            )
            # A malformed Core result must not alter the terminal business
            # outcome. Retry with durable state only to preserve lifecycle.
            try:
                return self.trace_normalizer.normalize(
                    run, result=None, failure=failure,
                )
            except Exception:
                logger.exception(
                    "Application Trace lifecycle fallback failed run_id=%s", run.id,
                )
                return ApplicationTraceNormalization([], False)

    def _persist_trace(self, run_id: UUID, events) -> None:
        for event in events:
            self.unit_of_work.trace_events.create(
                run_id,
                event.sequence,
                event.event_type.value,
                tool_name=event.tool_name,
                payload=event.database_payload(),
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


def _failure_for_exception(error_code: str, exc: Exception) -> ApplicationTraceFailure:
    if error_code in {"model_unavailable", "model_timeout", "model_failed"}:
        category = {
            "model_unavailable": PublicErrorCategory.CONFIGURATION,
            "model_timeout": PublicErrorCategory.TIMEOUT,
            "model_failed": PublicErrorCategory.UNKNOWN,
        }[error_code]
        return ApplicationTraceFailure(
            FailureStage.LLM,
            normalize_error(category.value, code=error_code),
        )
    if isinstance(exc, BackendError):
        return ApplicationTraceFailure(
            FailureStage.BACKEND,
            normalize_error(PublicErrorCategory.BACKEND_ERROR.value, code=error_code),
        )
    return ApplicationTraceFailure(
        FailureStage.AGENT_CORE,
        normalize_error(PublicErrorCategory.AGENT_ERROR.value, code=error_code),
    )


def _failure_for_result(result: Any) -> ApplicationTraceFailure:
    steps = list(getattr(result, "steps", []) or [])
    tool_results = list(getattr(result, "tool_results", []) or [])
    for index, step in enumerate(steps):
        if not bool(getattr(step, "required", False)) or index >= len(tool_results):
            continue
        tool_result = tool_results[index]
        if not bool(getattr(tool_result, "ok", False)):
            return ApplicationTraceFailure(
                FailureStage.TOOL,
                normalize_error(PublicErrorCategory.TOOL_ERROR.value, code="required_tool_failed"),
            )
    return ApplicationTraceFailure(
        FailureStage.AGENT_CORE,
        normalize_error(
            PublicErrorCategory.AGENT_ERROR.value,
            code="agent_run_failed",
        ),
    )
