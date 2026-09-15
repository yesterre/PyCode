from celery import shared_task

from backend.app.services.stale_reconciliation import (
    RECONCILE_STALE_RUNNING_CELERY_TASK_NAME,
)
from backend.app.tasks.runtime import worker_stale_running_reconciler


@shared_task(name=RECONCILE_STALE_RUNNING_CELERY_TASK_NAME, ignore_result=True)
def reconcile_stale_running_tasks() -> dict[str, int]:
    with worker_stale_running_reconciler() as reconciler:
        result = reconciler.reconcile()
    return {
        "scanned": result.scanned,
        "requeued": result.requeued,
        "completed": result.completed,
        "failed": result.failed,
        "deferred": result.deferred,
    }
