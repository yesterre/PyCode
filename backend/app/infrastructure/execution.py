from __future__ import annotations

import logging
from threading import Event, Lock, Thread

from backend.app.config import ExecutionLeaseSettings
from backend.app.core.errors import BackendError
from backend.app.core.models import BackgroundTask
from backend.app.db.session import DatabaseSessionManager
from backend.app.repositories import SqlAlchemyUnitOfWork


logger = logging.getLogger("pycode.backend.tasks")


class ExecutionOwnership:
    """Worker-side lease heartbeat kept strictly outside business/Agent Core."""

    def __init__(
        self,
        database: DatabaseSessionManager,
        task: BackgroundTask,
        settings: ExecutionLeaseSettings,
    ) -> None:
        if task.execution_token is None:
            raise ValueError("A claimed task must have an execution token.")
        self.database = database
        self.task_id = task.id
        self.task_type = task.task_type
        self.execution_token = task.execution_token
        self.worker_id = task.worker_id
        self.settings = settings
        self._stopped = Event()
        self._lost = Event()
        self._guard = Lock()
        self._thread: Thread | None = None

    def __enter__(self) -> "ExecutionOwnership":
        self._thread = Thread(
            target=self._run,
            name=f"pycode-task-heartbeat-{self.task_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=1)

    def ensure_owned(self) -> None:
        if self._lost.is_set():
            raise self._lost_error()
        try:
            renewed = self._heartbeat_once()
        except Exception:
            logger.exception(
                "Execution ownership check unavailable task_id=%s task_type=%s "
                "execution_token=%s",
                self.task_id, self.task_type, str(self.execution_token)[:8],
            )
            raise BackendError(
                "database_unavailable",
                "Database ownership could not be confirmed.",
            ) from None
        if not renewed:
            self._lost.set()
            raise self._lost_error()

    def _run(self) -> None:
        while not self._stopped.wait(self.settings.heartbeat_interval_seconds):
            try:
                renewed = self._heartbeat_once()
            except Exception:
                # A transient database outage is ownership-unknown, not immediate
                # proof that another execution owns the task.
                logger.exception(
                    "Execution heartbeat failed task_id=%s task_type=%s "
                    "execution_token=%s",
                    self.task_id, self.task_type, str(self.execution_token)[:8],
                )
                continue
            if not renewed:
                self._lost.set()
                logger.warning(
                    "Execution ownership lost task_id=%s task_type=%s "
                    "execution_token=%s",
                    self.task_id, self.task_type, str(self.execution_token)[:8],
                )
                return

    def _heartbeat_once(self) -> bool:
        # Serialize the background heartbeat and synchronous boundary check so
        # they never share or concurrently use a Session.
        with self._guard:
            session = self.database.open_session()
            try:
                uow = SqlAlchemyUnitOfWork(session)
                with uow.transaction():
                    task = uow.background_tasks.heartbeat(
                        self.task_id,
                        self.execution_token,
                        lease_ttl_seconds=self.settings.ttl_seconds,
                    )
                if task is not None:
                    logger.debug(
                        "Execution heartbeat renewed task_id=%s task_type=%s "
                        "worker_id=%s execution_token=%s lease_expires_at=%s",
                        self.task_id, self.task_type, self.worker_id,
                        str(self.execution_token)[:8], task.lease_expires_at,
                    )
                return task is not None
            finally:
                if session.in_transaction():
                    session.rollback()
                session.close()

    @staticmethod
    def _lost_error() -> BackendError:
        return BackendError(
            "worker_execution_lost",
            "Worker execution ownership was lost before completion.",
        )


class InlineExecutionOwnership:
    """Non-threaded owner guard used by in-memory/unit-test service graphs."""

    def __init__(self, task: BackgroundTask, check) -> None:
        if task.execution_token is None:
            raise ValueError("A claimed task must have an execution token.")
        self.task_id = task.id
        self.execution_token = task.execution_token
        self._check = check

    def __enter__(self) -> "InlineExecutionOwnership":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def ensure_owned(self) -> None:
        if not self._check(self.task_id, self.execution_token):
            raise BackendError(
                "worker_execution_lost",
                "Worker execution ownership was lost before completion.",
            )
