from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from uuid import UUID

from celery import Celery

from backend.app.config import (
    CeleryDeliverySettings, QueuedReconciliationSettings,
    StaleReconciliationSettings,
)
from backend.app.infrastructure.redis import RedisSettings
from backend.app.services.background_tasks import (
    AGENT_RUN_CELERY_TASK_NAME, HEALTH_CELERY_TASK_NAME,
    REPOSITORY_INDEX_CELERY_TASK_NAME,
)
from backend.app.services.queued_reconciliation import (
    RECONCILE_QUEUED_CELERY_TASK_NAME,
)
from backend.app.services.stale_reconciliation import (
    RECONCILE_STALE_RUNNING_CELERY_TASK_NAME,
)


def create_celery_app(redis_url: str | None = None) -> Celery:
    redis = RedisSettings(redis_url) if redis_url else RedisSettings.from_environment()
    delivery = CeleryDeliverySettings.from_environment()
    broker_url = redis.url
    app = Celery(
        "pycode", broker=broker_url,
        include=[
            "backend.app.tasks.health",
            "backend.app.tasks.indexing",
            "backend.app.tasks.agent_runs",
            "backend.app.tasks.reconciliation",
            "backend.app.tasks.stale_reconciliation",
        ],
    )
    app.conf.update(
        accept_content=["json"],
        broker_connection_timeout=redis.connect_timeout_seconds,
        broker_connection_retry_on_startup=True,
        broker_transport_options={
            "socket_connect_timeout": redis.connect_timeout_seconds,
            "socket_timeout": redis.socket_timeout_seconds,
            "visibility_timeout": delivery.visibility_timeout_seconds,
        },
        enable_utc=True,
        result_backend=None,
        task_default_queue="pycode",
        task_ignore_result=True,
        task_acks_late=True,
        task_acks_on_failure_or_timeout=True,
        task_reject_on_worker_lost=True,
        task_serializer="json",
        timezone="UTC",
        worker_prefetch_multiplier=1,
        beat_schedule={
            "reconcile-queued-background-tasks": {
                "task": RECONCILE_QUEUED_CELERY_TASK_NAME,
                "schedule": QueuedReconciliationSettings.from_environment().interval_seconds,
            },
            "reconcile-stale-running-background-tasks": {
                "task": RECONCILE_STALE_RUNNING_CELERY_TASK_NAME,
                "schedule": StaleReconciliationSettings.from_environment().interval_seconds,
            },
        },
    )
    return app


class CeleryTaskDispatcher:
    """Lazy producer adapter; constructing FastAPI does not connect to Redis."""

    def __init__(
        self, app_factory: Callable[[], Celery] | None = None,
    ) -> None:
        self._app_factory = app_factory or create_celery_app
        self._app: Celery | None = None
        self._guard = Lock()

    def enqueue_health(self, task_id: UUID) -> None:
        self._celery_app().send_task(
            HEALTH_CELERY_TASK_NAME, args=[str(task_id)], queue="pycode",
        )

    def enqueue_repository_index(self, task_id: UUID) -> None:
        self._celery_app().send_task(
            REPOSITORY_INDEX_CELERY_TASK_NAME,
            args=[str(task_id)], queue="pycode",
        )

    def enqueue_agent_run(self, task_id: UUID) -> None:
        self._celery_app().send_task(
            AGENT_RUN_CELERY_TASK_NAME,
            args=[str(task_id)], queue="pycode",
        )

    def _celery_app(self) -> Celery:
        if self._app is None:
            with self._guard:
                if self._app is None:
                    self._app = self._app_factory()
        return self._app
