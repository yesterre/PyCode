from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from backend.app.core.errors import BackendError
from backend.app.core.models import AgentRunStatus, BackgroundTaskStatus, IndexSummary
from backend.app.infrastructure.celery import CeleryTaskDispatcher, create_celery_app
from backend.app.repositories import InMemoryUnitOfWork
from backend.app.services.background_tasks import AGENT_RUN_CELERY_TASK_NAME
from pycode.engine import PyCodeEngine


@dataclass
class FakeTraceEvent:
    event_type: str
    timestamp: datetime
    message: str = ""
    status: str | None = None
    data: dict = field(default_factory=dict)


class UnprintableDebugValue:
    def __str__(self):
        raise RuntimeError("debug value cannot be serialized")


class FakeAgentResult:
    def __init__(self, *, ok: bool = True, answer: str | None = "Agent answer") -> None:
        self.ok = ok
        self.answer = answer
        self.trace = SimpleNamespace(
            run_id=str(uuid4()),
            events=[
                FakeTraceEvent(
                    "TurnStarted", datetime.now(timezone.utc),
                    "started", "running", {"turn_index": 1},
                ),
                FakeTraceEvent(
                    "PostToolUse", datetime.now(timezone.utc),
                    "finished", "ok", {
                        "tool": "retrieve_context",
                        "path": Path("main.py"),
                        "debug": UnprintableDebugValue(),
                    },
                ),
            ],
        )


def _submit_ready_agent(client, ready_url):
    project_id = ready_url.rsplit("/", 1)[-1]
    response = client.post(
        "/api/v1/agent-runs",
        json={"project_id": project_id, "task": "  analyze the entry point  "},
    )
    assert response.status_code == 202, response.text
    return response


def test_agent_submission_queues_linked_records_without_executing_core(
    client, ready_url, application, monkeypatch,
):
    def forbidden(*args, **kwargs):
        raise AssertionError("HTTP submission must not execute PyCodeEngine.run_agent()")

    monkeypatch.setattr(PyCodeEngine, "run_agent", forbidden)

    response = _submit_ready_agent(client, ready_url)
    body = response.json()
    run_id = UUID(body["run_id"])
    task_id = UUID(body["task_id"])
    run = application.state.persistence.agent_runs[run_id]
    task = application.state.persistence.background_tasks[task_id]

    assert body["status"] == "queued"
    assert run.status == AgentRunStatus.QUEUED
    assert run.started_at is None and run.finished_at is None
    assert task.status == BackgroundTaskStatus.QUEUED
    assert task.agent_run_id == run.id
    assert task.project_id == run.project_id
    assert task.snapshot_id == run.snapshot_id
    assert application.state.task_dispatcher.agent_run_task_ids == [task.id]
    assert client.get(f"/api/v1/tasks/{task.id}").json()["agent_run_id"] == str(run.id)


def test_agent_worker_calls_existing_entry_once_and_persists_result_and_trace(
    client, ready_url, application, agent_worker, monkeypatch,
):
    accepted = _submit_ready_agent(client, ready_url).json()
    task_id = UUID(accepted["task_id"])
    observed = []
    repository = application.state.workspace.path_for(
        UUID(ready_url.rsplit("/", 1)[-1]),
    )
    memory = repository / ".pclens" / "memory" / "keep.md"
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_text("keep", encoding="utf-8")

    def run_agent(root, task):
        current_run = application.state.persistence.agent_runs[UUID(accepted["run_id"])]
        current_task = application.state.persistence.background_tasks[task_id]
        assert current_run.status == AgentRunStatus.RUNNING
        assert current_task.status == BackgroundTaskStatus.RUNNING
        observed.append((Path(root), task))
        return FakeAgentResult()

    monkeypatch.setattr(application.state.adapter, "run_agent", run_agent)
    completed = agent_worker.execute(task_id)

    assert len(observed) == 1
    assert observed[0][1] == "analyze the entry point"
    assert completed.status == AgentRunStatus.COMPLETED
    assert completed.result == "Agent answer"
    assert completed.started_at is not None and completed.finished_at is not None
    background = application.state.persistence.background_tasks[task_id]
    assert background.status == BackgroundTaskStatus.COMPLETED
    assert background.started_at is not None and background.finished_at is not None
    events = sorted(
        application.state.persistence.trace_events.values(),
        key=lambda event: event.sequence,
    )
    assert [event.event_type for event in events] == ["TurnStarted", "PostToolUse"]
    assert all(event.run_id == completed.id for event in events)
    assert events[1].tool_name == "retrieve_context"
    assert events[1].payload["data"]["path"] == "main.py"
    assert events[1].payload["data"]["debug"] == "<unavailable>"
    root = observed[0][0]
    assert (root / ".pclens" / "index.json").is_file()
    assert (root / ".pclens" / "code_graph.json").is_file()
    assert (root / ".pclens" / "memory" / "keep.md").read_text(encoding="utf-8") == "keep"


def test_agent_worker_reaches_pycode_engine_run_agent_once(
    client, ready_url, application, agent_worker, monkeypatch,
):
    accepted = _submit_ready_agent(client, ready_url).json()
    task_id = UUID(accepted["task_id"])
    calls = []
    engine = application.state.adapter.engine
    original = engine.run_agent

    def observed_entry(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "run_agent", observed_entry)
    completed = agent_worker.execute(task_id)

    assert completed.status == AgentRunStatus.COMPLETED
    assert completed.result == "Offline HTTP answer"
    assert len(calls) == 1
    assert calls[0][0][1] == "analyze the entry point"
    assert calls[0][1] == {"llm_client": application.state.adapter.llm_client}


def test_agent_result_failure_marks_both_records_failed_with_safe_message(
    client, ready_url, application, agent_worker, monkeypatch,
):
    accepted = _submit_ready_agent(client, ready_url).json()
    task_id = UUID(accepted["task_id"])
    run_id = UUID(accepted["run_id"])
    monkeypatch.setattr(
        application.state.adapter,
        "run_agent",
        lambda root, task: FakeAgentResult(ok=False, answer="partial private output"),
    )

    with pytest.raises(BackendError) as failure:
        agent_worker.execute(task_id)

    assert failure.value.code == "agent_run_failed"
    run = application.state.persistence.agent_runs[run_id]
    background = application.state.persistence.background_tasks[task_id]
    assert run.status == AgentRunStatus.FAILED and run.result is None
    assert run.error_message == "Agent execution did not complete successfully."
    assert background.status == BackgroundTaskStatus.FAILED
    assert background.error_code == "agent_run_failed"
    assert background.error_message == run.error_message
    assert len(application.state.persistence.trace_events) == 2


def test_agent_exception_is_safely_persisted(
    client, ready_url, application, agent_worker, monkeypatch,
):
    accepted = _submit_ready_agent(client, ready_url).json()
    task_id = UUID(accepted["task_id"])
    run_id = UUID(accepted["run_id"])

    def crash(root, task):
        raise RuntimeError("private credential=secret")

    monkeypatch.setattr(application.state.adapter, "run_agent", crash)
    with pytest.raises(RuntimeError, match="private credential"):
        agent_worker.execute(task_id)

    run = application.state.persistence.agent_runs[run_id]
    background = application.state.persistence.background_tasks[task_id]
    assert run.status == AgentRunStatus.FAILED
    assert run.error_message == "Agent execution failed."
    assert "private" not in run.error_message
    assert background.status == BackgroundTaskStatus.FAILED
    assert background.error_message == run.error_message


def test_agent_worker_uses_snapshot_fixed_at_submission(
    client, ready_url, application, agent_worker, monkeypatch,
):
    accepted = _submit_ready_agent(client, ready_url).json()
    run_id = UUID(accepted["run_id"])
    task_id = UUID(accepted["task_id"])
    fixed_snapshot = application.state.persistence.agent_runs[run_id].snapshot_id

    uow = InMemoryUnitOfWork(application.state.persistence)
    with uow.transaction():
        project = uow.projects.get(UUID(ready_url.rsplit("/", 1)[-1]))
        newer = uow.snapshots.upsert(
            project.id,
            "b" * 40,
            index_artifact_path="artifacts/new/index.json",
            graph_artifact_path="artifacts/new/code_graph.json",
            summary=IndexSummary(1, 1, 0),
        )
        uow.projects.set_current_snapshot(project.id, newer.id)

    seen_snapshot_ids = []
    original = agent_worker.service().projects.execute_on_snapshot

    def record_snapshot(project_id, snapshot_id, operation):
        seen_snapshot_ids.append(snapshot_id)
        return original(project_id, snapshot_id, operation)

    service = agent_worker.service()
    monkeypatch.setattr(service.projects, "execute_on_snapshot", record_snapshot)
    monkeypatch.setattr(
        application.state.adapter, "run_agent", lambda root, task: FakeAgentResult(),
    )
    service.execute(task_id)

    assert seen_snapshot_ids == [fixed_snapshot]
    assert fixed_snapshot != newer.id


def test_agent_submission_enqueue_failure_keeps_both_queued(
    client, application, ready_url,
):
    class FailingDispatcher:
        def enqueue_health(self, task_id):
            raise AssertionError("not used")

        def enqueue_repository_index(self, task_id):
            raise AssertionError("not used")

        def enqueue_agent_run(self, task_id):
            raise RuntimeError("Redis unavailable")

    application.state.task_dispatcher = FailingDispatcher()
    response = client.post(
        "/api/v1/agent-runs",
        json={
            "project_id": ready_url.rsplit("/", 1)[-1],
            "task": "analyze entry",
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "task_queue_unavailable"
    assert len(application.state.persistence.agent_runs) == 1
    agent_tasks = [
        task for task in application.state.persistence.background_tasks.values()
        if task.task_type == "agent_run"
    ]
    assert len(agent_tasks) == 1
    assert next(iter(application.state.persistence.agent_runs.values())).status == "queued"
    assert agent_tasks[0].status == "queued"


def test_agent_run_query_validation_and_not_found(client, ready_url):
    accepted = _submit_ready_agent(client, ready_url).json()
    response = client.get(f"/api/v1/agent-runs/{accepted['run_id']}")
    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert response.json()["snapshot_id"] is not None
    assert client.get(f"/api/v1/agent-runs/{uuid4()}").status_code == 404
    assert client.get("/api/v1/agent-runs/not-a-uuid").status_code == 422


def test_agent_dispatcher_and_worker_adapter_are_thin(monkeypatch):
    class FakeCelery:
        def __init__(self):
            self.calls = []

        def send_task(self, name, *, args, queue):
            self.calls.append((name, args, queue))

    celery = FakeCelery()
    dispatcher = CeleryTaskDispatcher(app_factory=lambda: celery)
    task_id = uuid4()
    dispatcher.enqueue_agent_run(task_id)
    assert celery.calls == [(AGENT_RUN_CELERY_TASK_NAME, [str(task_id)], "pycode")]

    from backend.app.tasks import agent_runs

    called = []

    class FakeService:
        def execute(self, received):
            called.append(received)

    @contextmanager
    def fake_service():
        yield FakeService()

    monkeypatch.setattr(agent_runs, "worker_agent_run_service", fake_service)
    agent_runs.run_agent_task.run(str(task_id))
    assert called == [task_id]

    app = create_celery_app("redis://localhost:6379/15")
    app.loader.import_default_modules()
    assert AGENT_RUN_CELERY_TASK_NAME in app.tasks


def test_agent_run_repositories_reject_invalid_transitions(persistence):
    uow = InMemoryUnitOfWork(persistence)
    with uow.transaction():
        project = uow.projects.create("demo", "https://github.com/example/demo.git")
        run = uow.agent_runs.create_queued(
            project.id, None, task="demo", run_type="agent", model=None,
        )
        uow.agent_runs.mark_running(run.id)
        uow.agent_runs.complete(run.id, "done")

    with pytest.raises(BackendError) as failure:
        with uow.transaction():
            uow.agent_runs.mark_running(run.id)
    assert failure.value.code == "agent_run_state_conflict"
