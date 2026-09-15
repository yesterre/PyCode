from __future__ import annotations

from uuid import UUID

from backend.app.core.models import IndexSummary
from backend.app.repositories import InMemoryUnitOfWork
from backend.app.services.idempotency import (
    agent_run_fingerprint,
    repository_index_fingerprint,
    request_fingerprint,
)


def _agent_payload(project_url: str, task: str = "分析主要调用链") -> dict[str, str]:
    return {
        "project_id": project_url.rsplit("/", 1)[-1],
        "task": task,
    }


def _error_code(response) -> str:
    return response.json()["detail"]["code"]


def test_fingerprint_is_canonical_utf8_sha256():
    first = request_fingerprint({"task": "分析入口", "project_id": "p", "operation": "agent_run"})
    second = request_fingerprint({"operation": "agent_run", "project_id": "p", "task": "分析入口"})

    assert first == second
    assert len(first) == 64
    assert first != request_fingerprint({"operation": "agent_run", "project_id": "p", "task": "分析数据库"})
    assert repository_index_fingerprint("p") != agent_run_fingerprint("p", "分析入口")
    assert agent_run_fingerprint("p", "分析主要调用链") != agent_run_fingerprint(
        "p", "分析数据库模块",
    )


def test_repository_index_same_key_replays_and_enqueues_once(
    client, project_url, application,
):
    headers = {"Idempotency-Key": "index-key"}
    first = client.post(project_url + "/index", headers=headers)
    second = client.post(project_url + "/index", headers=headers)

    assert first.status_code == second.status_code == 202
    assert first.json()["task_id"] == second.json()["task_id"]
    assert len(application.state.persistence.background_tasks) == 1
    persisted = next(iter(application.state.persistence.background_tasks.values()))
    assert persisted.idempotency_key == "index-key"
    assert persisted.request_fingerprint == repository_index_fingerprint(
        project_url.rsplit("/", 1)[-1],
    )
    assert application.state.task_dispatcher.repository_index_task_ids == [
        UUID(first.json()["task_id"]),
    ]


def test_repository_index_terminal_replay_returns_old_current_status(
    client, project_url, application, index_worker,
):
    headers = {"Idempotency-Key": "terminal-index-key"}
    first = client.post(project_url + "/index", headers=headers)
    index_worker.execute(UUID(first.json()["task_id"]))

    replay = client.post(project_url + "/index", headers=headers)

    assert replay.status_code == 202
    assert replay.json()["task_id"] == first.json()["task_id"]
    assert replay.json()["status"] == "completed"
    assert len(application.state.task_dispatcher.repository_index_task_ids) == 1


def test_repository_index_active_queued_rejects_different_or_missing_key(
    client, project_url, application,
):
    first = client.post(
        project_url + "/index", headers={"Idempotency-Key": "index-a"},
    )
    different = client.post(
        project_url + "/index", headers={"Idempotency-Key": "index-b"},
    )
    missing = client.post(project_url + "/index")

    assert first.status_code == 202
    assert different.status_code == missing.status_code == 409
    assert _error_code(different) == _error_code(missing) == "repository_index_in_progress"
    assert len(application.state.persistence.background_tasks) == 1
    assert len(application.state.task_dispatcher.repository_index_task_ids) == 1


def test_repository_index_active_running_is_rejected(
    client, project_url, application,
):
    first = client.post(project_url + "/index")
    task_id = UUID(first.json()["task_id"])
    uow = InMemoryUnitOfWork(application.state.persistence)
    with uow.transaction():
        uow.background_tasks.claim(task_id)

    conflict = client.post(
        project_url + "/index", headers={"Idempotency-Key": "new-key"},
    )

    assert conflict.status_code == 409
    assert _error_code(conflict) == "repository_index_in_progress"
    assert len(application.state.task_dispatcher.repository_index_task_ids) == 1


def test_repository_index_terminal_allows_new_key(
    client, project_url, application, index_worker,
):
    first = client.post(
        project_url + "/index", headers={"Idempotency-Key": "old-key"},
    )
    index_worker.execute(UUID(first.json()["task_id"]))

    second = client.post(
        project_url + "/index", headers={"Idempotency-Key": "new-key"},
    )

    assert second.status_code == 202
    assert second.json()["task_id"] != first.json()["task_id"]
    assert len(application.state.task_dispatcher.repository_index_task_ids) == 2


def test_agent_same_key_replays_same_resources_and_enqueues_once(
    client, ready_url, application,
):
    headers = {"Idempotency-Key": "agent-key"}
    payload = _agent_payload(ready_url)
    first = client.post("/api/v1/agent-runs", json=payload, headers=headers)
    second = client.post("/api/v1/agent-runs", json=payload, headers=headers)

    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert len(application.state.persistence.agent_runs) == 1
    agent_tasks = [
        task for task in application.state.persistence.background_tasks.values()
        if task.task_type == "agent_run"
    ]
    assert len(agent_tasks) == 1
    assert agent_tasks[0].idempotency_key == "agent-key"
    assert agent_tasks[0].request_fingerprint == agent_run_fingerprint(
        ready_url.rsplit("/", 1)[-1], payload["task"],
    )
    assert application.state.task_dispatcher.agent_run_task_ids == [agent_tasks[0].id]


def test_agent_same_key_different_task_conflicts(
    client, ready_url, application,
):
    headers = {"Idempotency-Key": "agent-conflict"}
    first = client.post(
        "/api/v1/agent-runs", json=_agent_payload(ready_url, "分析入口"), headers=headers,
    )
    conflict = client.post(
        "/api/v1/agent-runs", json=_agent_payload(ready_url, "分析数据库"), headers=headers,
    )

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert _error_code(conflict) == "idempotency_conflict"
    assert len(application.state.persistence.agent_runs) == 1
    assert len(application.state.task_dispatcher.agent_run_task_ids) == 1


def test_agent_terminal_replay_returns_old_resources(
    client, ready_url, application, agent_worker,
):
    headers = {"Idempotency-Key": "completed-agent"}
    first = client.post(
        "/api/v1/agent-runs", json=_agent_payload(ready_url), headers=headers,
    )
    agent_worker.execute(UUID(first.json()["task_id"]))

    replay = client.post(
        "/api/v1/agent-runs", json=_agent_payload(ready_url), headers=headers,
    )

    assert replay.status_code == 202
    assert replay.json()["run_id"] == first.json()["run_id"]
    assert replay.json()["task_id"] == first.json()["task_id"]
    assert replay.json()["status"] == "completed"
    assert len(application.state.task_dispatcher.agent_run_task_ids) == 1


def test_agent_replay_remains_pinned_after_current_snapshot_changes(
    client, ready_url, application,
):
    project_id = UUID(ready_url.rsplit("/", 1)[-1])
    headers = {"Idempotency-Key": "snapshot-pinned-agent"}
    first = client.post(
        "/api/v1/agent-runs", json=_agent_payload(ready_url), headers=headers,
    )
    first_run = application.state.persistence.agent_runs[UUID(first.json()["run_id"])]
    uow = InMemoryUnitOfWork(application.state.persistence)
    with uow.transaction():
        second_snapshot = uow.snapshots.upsert(
            project_id,
            "b" * 40,
            index_artifact_path="artifacts/b/index.json",
            graph_artifact_path="artifacts/b/code_graph.json",
            summary=IndexSummary(1, 1, 0),
        )
        uow.projects.set_current_snapshot(project_id, second_snapshot.id)

    replay = client.post(
        "/api/v1/agent-runs", json=_agent_payload(ready_url), headers=headers,
    )
    replay_run = application.state.persistence.agent_runs[UUID(replay.json()["run_id"])]

    assert replay.status_code == 202
    assert replay.json()["run_id"] == first.json()["run_id"]
    assert replay_run.snapshot_id == first_run.snapshot_id
    assert replay_run.snapshot_id != second_snapshot.id


def test_agent_same_text_without_key_or_with_different_keys_creates_new_runs(
    client, ready_url, application,
):
    payload = _agent_payload(ready_url)
    no_key_a = client.post("/api/v1/agent-runs", json=payload)
    no_key_b = client.post("/api/v1/agent-runs", json=payload)
    keyed_a = client.post(
        "/api/v1/agent-runs", json=payload, headers={"Idempotency-Key": "text-a"},
    )
    keyed_b = client.post(
        "/api/v1/agent-runs", json=payload, headers={"Idempotency-Key": "text-b"},
    )

    assert {response.status_code for response in (no_key_a, no_key_b, keyed_a, keyed_b)} == {202}
    assert len({response.json()["run_id"] for response in (no_key_a, no_key_b, keyed_a, keyed_b)}) == 4
    assert len(application.state.persistence.agent_runs) == 4
    assert len(application.state.task_dispatcher.agent_run_task_ids) == 4


def test_same_key_is_scoped_by_project_and_task_type(
    client, ready_url, application, index_worker,
):
    created = client.post(
        "/api/v1/projects",
        json={"name": "Second", "repo_url": "https://github.com/example/demo.git"},
    )
    second_url = "/api/v1/projects/" + created.json()["id"]
    index_worker.run(client, second_url)
    headers = {"Idempotency-Key": "shared-key"}

    first_agent = client.post(
        "/api/v1/agent-runs", json=_agent_payload(ready_url), headers=headers,
    )
    second_agent = client.post(
        "/api/v1/agent-runs", json=_agent_payload(second_url), headers=headers,
    )
    index_same_key = client.post(ready_url + "/index", headers=headers)

    assert first_agent.status_code == second_agent.status_code == index_same_key.status_code == 202
    assert first_agent.json()["run_id"] != second_agent.json()["run_id"]
    assert len(application.state.task_dispatcher.agent_run_task_ids) == 2


def test_idempotency_key_validation_and_optional_compatibility(client, ready_url):
    payload = _agent_payload(ready_url)

    valid_without_header = client.post("/api/v1/agent-runs", json=payload)
    empty = client.post(
        "/api/v1/agent-runs", json=payload, headers={"Idempotency-Key": ""},
    )
    blank = client.post(
        "/api/v1/agent-runs", json=payload, headers={"Idempotency-Key": "   "},
    )
    oversized = client.post(
        "/api/v1/agent-runs", json=payload, headers={"Idempotency-Key": "x" * 256},
    )
    control = client.post(
        "/api/v1/agent-runs", json=payload, headers={"Idempotency-Key": "bad\x7fkey"},
    )

    assert valid_without_header.status_code == 202
    assert empty.status_code == blank.status_code == oversized.status_code == control.status_code == 422
