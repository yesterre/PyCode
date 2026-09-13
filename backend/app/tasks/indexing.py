from uuid import UUID

from celery import shared_task

from backend.app.services.background_tasks import REPOSITORY_INDEX_CELERY_TASK_NAME
from backend.app.tasks.runtime import worker_repository_index_services


@shared_task(name=REPOSITORY_INDEX_CELERY_TASK_NAME, ignore_result=True)
def run_repository_index_task(task_id: str) -> None:
    """Thin Celery adapter around the existing Phase 4 indexing use case."""
    with worker_repository_index_services() as (background_tasks, projects):
        background_tasks.execute_repository_index(UUID(task_id), projects.index)
