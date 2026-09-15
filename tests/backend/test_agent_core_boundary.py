from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from backend.app.core.errors import BackendError
from backend.app.core.models import (
    AgentCoreStartStatus, AgentRunStatus, BackgroundTaskStatus,
)
from backend.app.repositories import InMemoryUnitOfWork
from tests.backend.test_agent_run_tasks import FakeAgentResult, _submit_ready_agent


def test_new_agent_run_starts_before_core_boundary(client, ready_url, application):
    accepted = _submit_ready_agent(client, ready_url).json()
    run = application.state.persistence.agent_runs[UUID(accepted["run_id"])]
    assert run.status == AgentRunStatus.QUEUED
    assert run.core_started_at is None
    assert client.get(f"/api/v1/agent-runs/{run.id}").json()["core_started_at"] is None


def test_memory_marker_is_atomic_state_checked_and_irreversible(persistence):
    uow = InMemoryUnitOfWork(persistence)
    missing = uow.agent_runs.mark_core_started(uuid4())
    assert missing.status == AgentCoreStartStatus.NOT_FOUND
    with uow.transaction():
        project = uow.projects.create("demo", "https://github.com/example/demo.git")
        run = uow.agent_runs.create_queued(
            project.id, None, task="task", run_type="agent", model=None,
        )
        invalid = uow.agent_runs.mark_core_started(run.id)
    assert invalid.status == AgentCoreStartStatus.INVALID_STATE
    with uow.transaction():
        uow.agent_runs.mark_running(run.id)
        marked = uow.agent_runs.mark_core_started(run.id)
    assert marked.status == AgentCoreStartStatus.MARKED
    assert marked.run is not None and marked.run.core_started_at is not None
    original = marked.run.core_started_at
    with uow.transaction():
        again = uow.agent_runs.mark_core_started(run.id)
        failed = uow.agent_runs.fail(run.id, "safe")
    assert again.status == AgentCoreStartStatus.ALREADY_MARKED
    assert failed.core_started_at == original


def test_prepare_then_durable_marker_then_adapter(
    client, ready_url, application, agent_worker, monkeypatch,
):
    accepted = _submit_ready_agent(client, ready_url).json()
    task_id = UUID(accepted["task_id"])
    run_id = UUID(accepted["run_id"])
    order = []
    original_prepare = application.state.agent_workspace.prepare

    def prepare(*args, **kwargs):
        assert application.state.persistence.agent_runs[run_id].core_started_at is None
        order.append("prepare")
        return original_prepare(*args, **kwargs)

    def run_agent(root, task):
        persisted = application.state.persistence.agent_runs[run_id]
        assert persisted.core_started_at is not None
        order.append("adapter")
        return FakeAgentResult()

    monkeypatch.setattr(application.state.agent_workspace, "prepare", prepare)
    monkeypatch.setattr(application.state.adapter, "run_agent", run_agent)
    completed = agent_worker.execute(task_id)

    assert order == ["prepare", "adapter"]
    assert completed.status == AgentRunStatus.COMPLETED
    assert completed.core_started_at is not None
    assert application.state.persistence.background_tasks[task_id].attempt_count == 1


def test_marker_commit_failure_never_calls_adapter(
    client, ready_url, application, agent_worker, monkeypatch,
):
    accepted = _submit_ready_agent(client, ready_url).json()
    task_id = UUID(accepted["task_id"])
    run_id = UUID(accepted["run_id"])
    service = agent_worker.service()
    original_transaction = service.unit_of_work.transaction
    calls = []

    @contextmanager
    def fail_marker_commit():
        with original_transaction():
            yield
            run = application.state.persistence.agent_runs[run_id]
            if run.core_started_at is not None and run.status == AgentRunStatus.RUNNING:
                raise BackendError("database_failed", "A database transaction failed.")

    monkeypatch.setattr(service.unit_of_work, "transaction", fail_marker_commit)
    monkeypatch.setattr(
        application.state.adapter, "run_agent", lambda *args: calls.append(args),
    )
    with pytest.raises(BackendError) as failure:
        service.execute(task_id)
    assert failure.value.code == "database_failed"
    assert calls == []
    assert application.state.persistence.agent_runs[run_id].core_started_at is None
    assert application.state.persistence.agent_runs[run_id].status == AgentRunStatus.FAILED


@pytest.mark.parametrize("ok", [True, False])
def test_marker_survives_adapter_outcome_and_duplicate_delivery(
    client, ready_url, application, agent_worker, monkeypatch, ok,
):
    accepted = _submit_ready_agent(client, ready_url).json()
    task_id = UUID(accepted["task_id"])
    run_id = UUID(accepted["run_id"])
    calls = []

    def run_agent(*args):
        calls.append(args)
        if ok:
            return FakeAgentResult()
        return FakeAgentResult(ok=False)

    monkeypatch.setattr(application.state.adapter, "run_agent", run_agent)
    if ok:
        first = agent_worker.execute(task_id)
    else:
        with pytest.raises(BackendError):
            agent_worker.execute(task_id)
        first = application.state.persistence.agent_runs[run_id]
    marker = first.core_started_at
    assert marker is not None
    duplicate = agent_worker.execute(task_id)
    assert duplicate.core_started_at == marker
    assert len(calls) == 1


def test_existing_core_marker_blocks_transparent_entry(
    client, ready_url, application, agent_worker, monkeypatch,
):
    accepted = _submit_ready_agent(client, ready_url).json()
    task_id = UUID(accepted["task_id"])
    run_id = UUID(accepted["run_id"])
    application.state.persistence.agent_runs[run_id] = replace(
        application.state.persistence.agent_runs[run_id],
        core_started_at=datetime.now(timezone.utc),
    )
    calls = []
    monkeypatch.setattr(
        application.state.adapter, "run_agent", lambda *args: calls.append(args),
    )
    with pytest.raises(BackendError) as failure:
        agent_worker.execute(task_id)
    assert failure.value.code == "background_task_consistency_error"
    assert calls == []
    run = application.state.persistence.agent_runs[run_id]
    task = application.state.persistence.background_tasks[task_id]
    assert run.status == AgentRunStatus.FAILED
    assert run.core_started_at is not None
    assert task.status == BackgroundTaskStatus.FAILED
