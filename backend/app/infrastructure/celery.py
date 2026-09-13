from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from uuid import UUID

from celery import Celery

from backend.app.infrastructure.redis import RedisSettings
from backend.app.services.background_tasks import (
    HEALTH_CELERY_TASK_NAME, REPOSITORY_INDEX_CELERY_TASK_NAME,
)


def create_celery_app(redis_url: str | None = None) -> Celery:
    broker_url = redis_url or RedisSettings.from_environment().url
    app = Celery(
        "pycode", broker=broker_url,
        include=["backend.app.tasks.health", "backend.app.tasks.indexing"],
    )
    app.conf.update(
        accept_content=["json"],
        broker_connection_retry_on_startup=True,
        enable_utc=True,
        result_backend=None,
        task_default_queue="pycode",
        task_ignore_result=True,
        task_serializer="json",
        timezone="UTC",
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

    def _celery_app(self) -> Celery:
        if self._app is None:
            with self._guard:
                if self._app is None:
                    self._app = self._app_factory()
        return self._app
