from uuid import UUID

from celery import shared_task

from backend.app.services.background_tasks import AGENT_RUN_CELERY_TASK_NAME
from backend.app.tasks.runtime import worker_agent_run_service


@shared_task(name=AGENT_RUN_CELERY_TASK_NAME, ignore_result=True)
def run_agent_task(task_id: str) -> None:
    """Thin Celery adapter around the asynchronous AgentRun application service."""
    with worker_agent_run_service() as service:
        service.execute(UUID(task_id))
