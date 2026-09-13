from __future__ import annotations

import logging
from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.core.errors import BackendError
from backend.app.core.models import BackgroundTaskStatus
from backend.app.infrastructure.celery import CeleryTaskDispatcher, create_celery_app
from backend.app.infrastructure.redis import RedisSettings
from backend.app.main import create_app
from backend.app.repositories import InMemoryPersistence, InMemoryUnitOfWork
from backend.app.services.background_tasks import (
    HEALTH_CELERY_TASK_NAME, BackgroundTaskService,
)


class RecordingDispatcher:
    def __init__(self) -> None:
        self.task_ids: list[UUID] = []

    def enqueue_health(self, task_id: UUID) -> None:
        self.task_ids.append(task_id)


def _service(
    persistence: InMemoryPersistence | None = None,
    dispatcher: RecordingDispatcher | None = None,
) -> tuple[BackgroundTaskService, InMemoryPersistence, RecordingDispatcher]:
    state = persistence or InMemoryPersistence()
    queue = dispatcher or RecordingDispatcher()
    return BackgroundTaskService(InMemoryUnitOfWork(state), queue), state, queue


def test_background_task_create_starts_queued_and_enqueues_after_persistence():
    service, state, dispatcher = _service()

    task = service.create_health_task()

    assert task.status == BackgroundTaskStatus.QUEUED
    assert task.attempt_count == 0
    assert task.started_at is None and task.finished_at is None
    assert task.error_code is None and task.error_message is None
    assert dispatcher.task_ids == [task.id]
    assert state.background_tasks[task.id] == task


def test_enqueue_failure_is_safe_and_leaves_persisted_task_for_diagnosis(caplog):
    class FailingDispatcher:
        def enqueue_health(self, task_id: UUID) -> None:
            raise RuntimeError("redis://user:private-password@localhost:6379/0")

    state = InMemoryPersistence()
    service = BackgroundTaskService(InMemoryUnitOfWork(state), FailingDispatcher())

    with caplog.at_level(logging.ERROR, logger="pycode.backend.tasks"):
        with pytest.raises(BackendError) as failure:
            service.create_health_task()

    assert failure.value.code == "task_queue_unavailable"
    assert len(state.background_tasks) == 1
    assert next(iter(state.background_tasks.values())).status == BackgroundTaskStatus.QUEUED
    assert "private-password" not in caplog.text


def test_background_task_queued_running_completed_lifecycle():
    service, state, _ = _service()
    task = service.create_health_task()
    observed: list[BackgroundTaskStatus] = []

    def operation() -> None:
        observed.append(state.background_tasks[task.id].status)

    completed = service.execute_health(task.id, operation)

    assert observed == [BackgroundTaskStatus.RUNNING]
    assert completed.status == BackgroundTaskStatus.COMPLETED
    assert completed.attempt_count == 1
    assert completed.started_at is not None and completed.finished_at is not None
    assert completed.started_at <= completed.finished_at
    assert completed.error_code is None and completed.error_message is None


def test_background_task_exception_marks_failed_and_persists_safe_error():
    service, _, _ = _service()
    task = service.create_health_task()

    def fail() -> None:
        raise RuntimeError("secret implementation detail")

    with pytest.raises(RuntimeError, match="secret implementation detail"):
        service.execute_health(task.id, fail)

    failed = service.get(task.id)
    assert failed.status == BackgroundTaskStatus.FAILED
    assert failed.attempt_count == 1
    assert failed.started_at is not None and failed.finished_at is not None
    assert failed.error_code == "background_task_failed"
    assert failed.error_message == "Background task execution failed."
    assert "secret" not in failed.error_message


def test_background_task_backend_error_keeps_public_code_and_message():
    service, _, _ = _service()
    task = service.create_health_task()

    def fail() -> None:
        raise BackendError("health_probe_failed", "The health probe failed safely.")

    with pytest.raises(BackendError):
        service.execute_health(task.id, fail)

    failed = service.get(task.id)
    assert failed.error_code == "health_probe_failed"
    assert failed.error_message == "The health probe failed safely."


def test_background_task_rejects_invalid_state_transition():
    service, _, _ = _service()
    task = service.create_health_task()
    service.execute_health(task.id)

    with pytest.raises(BackendError) as failure:
        service.execute_health(task.id)

    assert failure.value.code == "background_task_state_conflict"
    assert service.get(task.id).status == BackgroundTaskStatus.COMPLETED


def test_task_http_create_and_query_without_real_redis():
    dispatcher = RecordingDispatcher()
    persistence = InMemoryPersistence()
    app = create_app(
        persistence=persistence, task_dispatcher=dispatcher,
    )
    with TestClient(app) as client:
        created = client.post("/api/v1/tasks/health")
        assert created.status_code == 202
        body = created.json()
        assert body["status"] == "queued" and body["task_type"] == "health"
        assert body["attempt_count"] == 0
        assert body["started_at"] is None and body["finished_at"] is None
        assert body["error_code"] is None and body["error_message"] is None
        assert client.get(f"/api/v1/tasks/{body['id']}").json() == body
        BackgroundTaskService(InMemoryUnitOfWork(persistence)).execute_health(UUID(body["id"]))
        completed = client.get(f"/api/v1/tasks/{body['id']}").json()
        assert completed["status"] == "completed"
        assert completed["attempt_count"] == 1
        assert completed["started_at"] is not None and completed["finished_at"] is not None
        assert client.get(f"/api/v1/tasks/{uuid4()}").status_code == 404
        assert client.get("/api/v1/tasks/not-a-uuid").status_code == 422
    assert dispatcher.task_ids == [UUID(body["id"])]


def test_redis_settings_and_celery_configuration_are_environment_driven(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://cache.internal:6380/2")
    settings = RedisSettings.from_environment()
    app = create_celery_app()

    assert settings.url == "redis://cache.internal:6380/2"
    assert app.conf.broker_url == settings.url
    assert app.conf.result_backend is None
    assert app.conf.task_ignore_result is True
    assert "backend.app.tasks.health" in app.conf.include


def test_celery_dispatcher_sends_named_json_task_without_connecting():
    class FakeCelery:
        def __init__(self) -> None:
            self.calls = []

        def send_task(self, name, *, args, queue):
            self.calls.append((name, args, queue))

    celery = FakeCelery()
    dispatcher = CeleryTaskDispatcher(app_factory=lambda: celery)  # type: ignore[arg-type]
    task_id = uuid4()

    dispatcher.enqueue_health(task_id)

    assert celery.calls == [(HEALTH_CELERY_TASK_NAME, [str(task_id)], "pycode")]


def test_health_celery_task_is_a_thin_service_adapter(monkeypatch):
    from backend.app.tasks import health

    called: list[UUID] = []

    class FakeService:
        def execute_health(self, task_id: UUID) -> None:
            called.append(task_id)

    @contextmanager
    def fake_service():
        yield FakeService()

    monkeypatch.setattr(health, "worker_background_task_service", fake_service)
    task_id = uuid4()
    health.run_health_task.run(str(task_id))
    assert called == [task_id]
