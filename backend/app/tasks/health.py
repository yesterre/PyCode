import os
from uuid import UUID

from celery import shared_task
from celery.exceptions import Reject

from backend.app.core.errors import BackendError
from backend.app.services.background_tasks import HEALTH_CELERY_TASK_NAME
from backend.app.tasks.runtime import worker_background_task_service


@shared_task(bind=True, name=HEALTH_CELERY_TASK_NAME, ignore_result=True)
def run_health_task(self, task_id: str) -> None:
    """Thin Celery adapter for the Phase 5A infrastructure health task."""
    try:
        with worker_background_task_service() as service:
            worker_id = f"{self.request.hostname}:{os.getpid()}:{self.request.id}"
            service.execute_health(UUID(task_id), worker_id=worker_id)
    except BackendError as exc:
        if exc.code == "database_unavailable":
            raise Reject(requeue=True) from exc
        raise
