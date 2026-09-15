import os
from uuid import UUID

from celery import shared_task
from celery.exceptions import Reject

from backend.app.core.errors import BackendError
from backend.app.services.background_tasks import AGENT_RUN_CELERY_TASK_NAME
from backend.app.tasks.runtime import worker_agent_run_service


@shared_task(bind=True, name=AGENT_RUN_CELERY_TASK_NAME, ignore_result=True)
def run_agent_task(self, task_id: str) -> None:
    """Thin Celery adapter around the asynchronous AgentRun application service."""
    try:
        with worker_agent_run_service() as service:
            worker_id = f"{self.request.hostname}:{os.getpid()}:{self.request.id}"
            service.execute(UUID(task_id), worker_id=worker_id)
    except BackendError as exc:
        if exc.code == "database_unavailable":
            raise Reject(requeue=True) from exc
        raise
