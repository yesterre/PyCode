from __future__ import annotations

from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.core.models import BackgroundTaskStatus, ProjectStatus
from backend.app.infrastructure.celery import CeleryTaskDispatcher, create_celery_app
from backend.app.main import create_app
from backend.app.repositories import InMemoryPersistence
from backend.app.services.background_tasks import REPOSITORY_INDEX_CELERY_TASK_NAME
from pycode.storage import load_graph, load_index


def test_index_submission_is_queued_and_does_not_execute_indexing(
    client, project_url, application, monkeypatch,
):
    from backend.app.services.projects import ProjectService

    def forbidden(*args, **kwargs):
        raise AssertionError("HTTP submission must not execute ProjectService.index()")

    monkeypatch.setattr(ProjectService, "index", forbidden)

    response = client.post(project_url + "/index")

    assert response.status_code == 202
    body = response.json()
    assert body == {
        "task_id": body["task_id"],
        "project_id": project_url.rsplit("/", 1)[-1],
        "status": "queued",
    }
    task = client.get(f"/api/v1/tasks/{body['task_id']}")
    assert task.status_code == 200
    assert task.json()["task_type"] == "repository_index"
    assert task.json()["project_id"] == body["project_id"]
    assert task.json()["attempt_count"] == 0
    assert application.state.task_dispatcher.repository_index_task_ids == [
        UUID(body["task_id"]),
    ]
    assert application.state.workspace.source.calls == []


def test_index_worker_completes_task_and_links_phase4_snapshot(
    client, project_url, repository, index_worker, application,
):
    accepted = index_worker.submit(client, project_url)
    task_id = UUID(accepted.json()["task_id"])

    completed = index_worker.execute(task_id)

    assert completed.status == BackgroundTaskStatus.COMPLETED
    assert completed.attempt_count == 1
    assert completed.started_at is not None
    assert completed.finished_at is not None
    assert completed.snapshot_id is not None
    task = client.get(f"/api/v1/tasks/{task_id}").json()
    project = client.get(project_url).json()
    assert task["status"] == "completed"
    persisted_project = application.state.persistence.projects[
        UUID(project_url.rsplit("/", 1)[-1])
    ]
    assert task["snapshot_id"] == str(persisted_project.current_snapshot_id)
    assert project["status"] == "ready"
    artifact_dir = next((repository.parent / "artifacts").iterdir())
    index = load_index(artifact_dir / "index.json")
    graph = load_graph(artifact_dir / "code_graph.json")
    assert project["index_summary"] == {
        "file_count": len(index.files),
        "node_count": len(graph.nodes),
        "edge_count": len(graph.edges),
    }


def test_index_submission_rejects_unknown_project_without_queuing(client, application):
    response = client.post(f"/api/v1/projects/{uuid4()}/index")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "project_not_found"
    assert application.state.persistence.background_tasks == {}
    assert application.state.task_dispatcher.repository_index_task_ids == []


def test_index_enqueue_failure_returns_503_and_retains_queued_task(
    application, project_url,
):
    class FailingDispatcher:
        def enqueue_health(self, task_id: UUID) -> None:
            raise AssertionError("not used")

        def enqueue_repository_index(self, task_id: UUID) -> None:
            raise RuntimeError("Redis unavailable")

    application.state.task_dispatcher = FailingDispatcher()
    with TestClient(application) as client:
        response = client.post(project_url + "/index")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "task_queue_unavailable"
    assert len(application.state.persistence.background_tasks) == 1
    task = next(iter(application.state.persistence.background_tasks.values()))
    assert task.status == BackgroundTaskStatus.QUEUED
    assert task.project_id == UUID(project_url.rsplit("/", 1)[-1])


def test_index_worker_failure_persists_safe_task_error(
    client, project_url, application, index_worker, monkeypatch,
):
    def crash(*args, **kwargs):
        raise RuntimeError("private indexing implementation detail")

    monkeypatch.setattr(application.state.adapter, "index", crash)
    accepted = index_worker.submit(client, project_url)
    task_id = UUID(accepted.json()["task_id"])

    with pytest.raises(RuntimeError, match="private indexing implementation detail"):
        index_worker.execute(task_id)

    task = client.get(f"/api/v1/tasks/{task_id}").json()
    project = client.get(project_url).json()
    assert task["status"] == "failed"
    assert task["attempt_count"] == 1
    assert task["error_code"] == "internal_error"
    assert task["error_message"] == "An unexpected server error occurred."
    assert task["finished_at"] is not None
    assert "private" not in task["error_message"]
    assert project["status"] == ProjectStatus.FAILED


def test_index_dispatcher_sends_stable_named_task():
    class FakeCelery:
        def __init__(self) -> None:
            self.calls = []

        def send_task(self, name, *, args, queue):
            self.calls.append((name, args, queue))

    celery = FakeCelery()
    dispatcher = CeleryTaskDispatcher(app_factory=lambda: celery)  # type: ignore[arg-type]
    task_id = uuid4()

    dispatcher.enqueue_repository_index(task_id)

    assert celery.calls == [
        (REPOSITORY_INDEX_CELERY_TASK_NAME, [str(task_id)], "pycode"),
    ]


def test_index_celery_task_is_a_thin_service_adapter(monkeypatch):
    from backend.app.tasks import indexing

    called: list[tuple[UUID, object]] = []

    class FakeBackgroundTasks:
        def execute_repository_index(self, task_id: UUID, operation) -> None:
            called.append((task_id, operation))

    class FakeProjects:
        def index(self, project_id: UUID):
            raise AssertionError("The adapter must pass, not invoke, the use case")

    projects = FakeProjects()

    @contextmanager
    def fake_services():
        yield FakeBackgroundTasks(), projects

    monkeypatch.setattr(indexing, "worker_repository_index_services", fake_services)
    task_id = uuid4()

    indexing.run_repository_index_task.run(str(task_id))

    assert called == [(task_id, projects.index)]


def test_worker_registers_health_and_repository_index_tasks():
    app = create_celery_app("redis://localhost:6379/15")
    app.loader.import_default_modules()

    assert "pycode.tasks.health" in app.tasks
    assert REPOSITORY_INDEX_CELERY_TASK_NAME in app.tasks


def test_phase5a_health_endpoint_remains_queued_without_real_redis():
    class RecordingDispatcher:
        def __init__(self) -> None:
            self.health_task_ids: list[UUID] = []

        def enqueue_health(self, task_id: UUID) -> None:
            self.health_task_ids.append(task_id)

        def enqueue_repository_index(self, task_id: UUID) -> None:
            raise AssertionError("not used")

    dispatcher = RecordingDispatcher()
    app = create_app(
        persistence=InMemoryPersistence(), task_dispatcher=dispatcher,
    )
    with TestClient(app) as client:
        response = client.post("/api/v1/tasks/health")

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert dispatcher.health_task_ids == [UUID(response.json()["id"])]
