from uuid import UUID

from celery import shared_task

from backend.app.services.background_tasks import HEALTH_CELERY_TASK_NAME
from backend.app.tasks.runtime import worker_background_task_service


@shared_task(name=HEALTH_CELERY_TASK_NAME, ignore_result=True)
def run_health_task(task_id: str) -> None:
    """Thin Celery adapter for the Phase 5A infrastructure health task."""
    with worker_background_task_service() as service:
        service.execute_health(UUID(task_id))
