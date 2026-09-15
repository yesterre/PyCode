from celery import shared_task

from backend.app.services.queued_reconciliation import (
    RECONCILE_QUEUED_CELERY_TASK_NAME,
)
from backend.app.tasks.runtime import worker_queued_task_reconciler


@shared_task(name=RECONCILE_QUEUED_CELERY_TASK_NAME, ignore_result=True)
def reconcile_queued_tasks() -> None:
    """Thin periodic adapter around queued broker publish reconciliation."""
    with worker_queued_task_reconciler() as reconciler:
        reconciler.run()
