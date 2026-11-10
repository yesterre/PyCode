from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from backend.app.core.run_metadata import preflight_execution, question_metadata
from backend.app.repositories import InMemoryUnitOfWork
from pycode.question import StrategyDecision


def _project(client, name: str) -> UUID:
    response = client.post("/api/v1/projects", json={
        "name": name,
        "repo_url": "https://github.com/example/demo.git",
    })
    assert response.status_code == 201
    return UUID(response.json()["id"])


def _completed(uow, project_id: UUID, run_type: str, task: str, answer: str | None):
    run = uow.agent_runs.create(
        project_id, None, task=task, run_type=run_type, model="internal-model",
    )
    return uow.agent_runs.complete(run.id, answer, prompt_tokens=12, completion_tokens=4)


def test_analysis_history_not_found_and_query_validation(client):
    missing = client.get(f"/api/v1/projects/{uuid4()}/analysis-history")
    assert missing.status_code == 404
    project_id = _project(client, "Validation")
    for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
        assert client.get(
            f"/api/v1/projects/{project_id}/analysis-history", params=params,
        ).status_code == 422


def test_analysis_history_filters_maps_orders_pages_and_exposes_minimal_fields(
    client, persistence,
):
    first_id = _project(client, "First")
    other_id = _project(client, "Other")
    uow = InMemoryUnitOfWork(persistence)
    anchor = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)

    with uow.transaction():
        ask = _completed(uow, first_id, "ask", "Where is the entry point?", "In main.py")
        impact = _completed(uow, first_id, "impact", "src/service.py", "The API changes")
        agent = _completed(uow, first_id, "agent", "Explain the flow", "Request flow answer")
        _completed(uow, other_id, "ask", "Foreign question", "Foreign answer")

        uow.agent_runs.create_queued(
            first_id, None, task="Queued", run_type="agent", model=None,
        )
        uow.agent_runs.create(
            first_id, None, task="Running", run_type="agent", model=None,
        )
        failed = uow.agent_runs.create(
            first_id, None, task="Failed", run_type="ask", model=None,
        )
        uow.agent_runs.fail(failed.id, "safe failure")
        _completed(uow, first_id, "impact", "No answer", None)
        _completed(uow, first_id, "unknown", "Unknown", "Unknown answer")

        persistence.agent_runs[ask.id] = replace(ask, created_at=anchor)
        persistence.agent_runs[impact.id] = replace(impact, created_at=anchor + timedelta(minutes=1))
        persistence.agent_runs[agent.id] = replace(agent, created_at=anchor + timedelta(minutes=1))

    expected = sorted(
        (persistence.agent_runs[impact.id], persistence.agent_runs[agent.id]),
        key=lambda run: str(run.id), reverse=True,
    ) + [persistence.agent_runs[ask.id]]
    url = f"/api/v1/projects/{first_id}/analysis-history"

    first = client.get(url, params={"limit": 2, "offset": 0})
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["has_more"] is True
    assert [item["id"] for item in first_body["items"]] == [str(run.id) for run in expected[:2]]

    second = client.get(url, params={"limit": 2, "offset": 2})
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["has_more"] is False
    assert [item["id"] for item in second_body["items"]] == [str(expected[2].id)]

    combined = first_body["items"] + second_body["items"]
    assert {item["mode"] for item in combined} == {"repository", "impact", "agent"}
    by_mode = {item["mode"]: item for item in combined}
    assert by_mode["repository"]["question"] == "Where is the entry point?"
    assert by_mode["repository"]["answer"] == "In main.py"
    assert by_mode["impact"]["question"] == "src/service.py"
    assert by_mode["impact"]["answer"] == "The API changes"
    assert by_mode["agent"]["question"] == "Explain the flow"
    assert by_mode["agent"]["answer"] == "Request flow answer"
    assert all(set(item) == {"id", "created_at", "mode", "question", "answer"}
               for item in combined)
    assert not any(item["question"] in {
        "Foreign question", "Queued", "Running", "Failed", "No answer", "Unknown",
    } for item in combined)


@pytest.mark.parametrize(("strategy", "preflight_status", "expected_mode"), [
    pytest.param("fast", None, "repository", id="fast"),
    pytest.param("agent", None, "agent", id="agent"),
    pytest.param("fast", "insufficient", "agent", id="fast-upgraded-to-agent"),
    pytest.param("fast", "sufficient", "repository", id="fast-after-preflight"),
])
def test_question_history_uses_final_execution_strategy(
    client, persistence, strategy, preflight_status, expected_mode,
):
    project_id = _project(client, "Question strategy")
    uow = InMemoryUnitOfWork(persistence)
    question = "Investigate the cross-module call chain"
    with uow.transaction():
        snapshot = uow.snapshots.start(project_id, "a" * 40)
        decision = StrategyDecision(
            strategy, "general", None, ("default_agent" if strategy == "agent" else "simple_orientation",),
            "multi_step" if strategy == "agent" else "snapshot_evidence",
        )
        request, execution = question_metadata(snapshot, decision, None)
        if preflight_status is not None:
            execution = preflight_execution(execution, {
                "status": preflight_status,
                "reasons": ["evidence_empty"] if preflight_status == "insufficient" else [],
                "pack_digest": "f" * 64,
                "selected_count": 0 if preflight_status == "insufficient" else 1,
                "discarded_count": 0, "budget": 12000, "estimated_tokens": 100,
            })
            assert execution["initial_strategy"] == "fast"
        run = uow.agent_runs.create(
            project_id, snapshot.id, task=question, run_type="question", model=None,
            request_metadata=request, execution_metadata=execution,
        )
        uow.agent_runs.complete(run.id, "Persisted answer")

    response = client.get(f"/api/v1/projects/{project_id}/analysis-history")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"items", "has_more"}
    assert body["has_more"] is False
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert set(item) == {"id", "created_at", "mode", "question", "answer"}
    assert item["id"] == str(run.id)
    assert item["mode"] == expected_mode
    assert item["question"] == question
    assert item["answer"] == "Persisted answer"


@pytest.mark.parametrize("metadata", [
    pytest.param(None, id="missing-metadata"),
    pytest.param({}, id="missing-strategy"),
    pytest.param({"initial_strategy": "agent"}, id="initial-strategy-only"),
    pytest.param({"strategy": None}, id="null-strategy"),
    pytest.param({"strategy": "unknown"}, id="unknown-strategy"),
    pytest.param({"strategy": ["agent"]}, id="list-strategy"),
    pytest.param({"strategy": {"name": "agent"}}, id="object-strategy"),
    pytest.param("agent", id="string-metadata"),
    pytest.param(["agent"], id="list-metadata"),
    pytest.param(42, id="number-metadata"),
])
def test_question_history_falls_back_for_missing_or_unrecognized_strategy(
    client, persistence, metadata,
):
    project_id = _project(client, "Legacy question")
    uow = InMemoryUnitOfWork(persistence)
    with uow.transaction():
        run = _completed(uow, project_id, "ask", "Investigate with Agent", "Legacy answer")
        # Seed historical/malformed stored data without weakening new Run validation.
        persistence.agent_runs[run.id] = replace(
            run, run_type="question", execution_metadata=metadata,
        )

    response = client.get(f"/api/v1/projects/{project_id}/analysis-history")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["has_more"] is False
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["id"] == str(run.id)
    assert item["mode"] == "repository"
    assert item["question"] == "Investigate with Agent"
    assert item["answer"] == "Legacy answer"
