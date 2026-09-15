import os
from uuid import UUID

from celery import shared_task
from celery.exceptions import Reject

from backend.app.core.errors import BackendError
from backend.app.services.background_tasks import REPOSITORY_INDEX_CELERY_TASK_NAME
from backend.app.tasks.runtime import worker_repository_index_services


@shared_task(bind=True, name=REPOSITORY_INDEX_CELERY_TASK_NAME, ignore_result=True)
def run_repository_index_task(self, task_id: str) -> None:
    """Thin Celery adapter around the existing Phase 4 indexing use case."""
    try:
        with worker_repository_index_services() as (background_tasks, projects):
            worker_id = f"{self.request.hostname}:{os.getpid()}:{self.request.id}"

            def owned_operation(task, ownership):
                if task.execution_token is None or task.project_id is None:
                    raise RuntimeError("Claimed Repository task has no execution owner.")
                if task.snapshot_id is not None:
                    return projects.recover_index_for_snapshot(
                        task.project_id,
                        task.snapshot_id,
                        task_id=task.id,
                        execution_token=task.execution_token,
                        execution=ownership,
                    )
                return projects.index(
                    task.project_id,
                    task_id=task.id,
                    execution_token=task.execution_token,
                    execution=ownership,
                )

            background_tasks.execute_repository_index(
                UUID(task_id), projects.index,
                owned_operation=owned_operation,
                worker_id=worker_id,
            )
    except BackendError as exc:
        if exc.code == "database_unavailable":
            raise Reject(requeue=True) from exc
        raise
