"""End-to-end A02a evidence through the production Services and Adapter."""
from dataclasses import replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from backend.app.core.application_trace import (
    ApplicationEventType, ApplicationTraceEvent, ApplicationTraceNormalizer,
    public_retrieval_summary,
)
from backend.app.core.models import AgentRunStatus
from backend.app.core.realtime_trace import RealtimeTraceSession
from backend.app.repositories import InMemoryUnitOfWork
from pycode.agent.trace import ToolTrace
from pycode.retriever import retrieve_for_question
from pycode.tools import ToolContext, retrieve_context


@pytest.fixture
def late_project(source_repository, client, project_url, index_worker):
    text = "# beyond former file-head cutoff\n" * 100 + "def serve():\n"
    text += "    value = 1\n" * 125 + "    return 'original-snapshot-marker'\n"
    (source_repository / "service.py").write_text(text, encoding="utf-8")
    indexed = index_worker.run(client, project_url)
    assert indexed.status_code == 200
    return SimpleNamespace(url=project_url, project_id=UUID(project_url.rsplit("/", 1)[-1]),
                           snapshot_id=UUID(indexed.json()["current_snapshot_id"]), source=text)


def test_fast_path_api_and_tool_use_same_evidence_and_complete_prompt(late_project, client, application, monkeypatch):
    observed = []
    original = application.state.adapter.engine.ask
    def ask(*args, **kwargs):
        result = original(*args, **kwargs)
        observed.append((kwargs["retrieval_context"], result))
        return result
    monkeypatch.setattr(application.state.adapter.engine, "ask", ask)
    response = client.post(late_project.url + "/ask", json={"question": "serve"})
    assert response.status_code == 200, response.text
    context, result = observed[0]
    tool = retrieve_context(ToolContext(context.root, retrieval_context=context), "serve")
    body = response.json()
    assert body["evidence"] == result.evidence == tool.data["evidence"]
    assert body["retrieval"] == result.retrieval.summary()
    assert body["retrieval"]["binding"] == {
        "project_id": str(late_project.project_id), "snapshot_id": str(late_project.snapshot_id), "commit_sha": "a" * 40,
    }
    assert [{k: v for k, v in item.items() if k != "snippet_truncated"} for item in tool.data["items"]] == [
        item.to_dict() for item in result.retrieval.items]
    item = result.retrieval.items[0]
    assert item.symbol == "serve" and item.start_line == 101 and item.end_line == 227
    assert len(item.chunk_ids) > 1 and item.coverage == "complete_symbol"
    assert item.snippet in application.state.adapter.llm_client.prompts[-1]


def test_impact_api_preserves_target_and_reverse_import_strategy(late_project, client):
    body = client.post(late_project.url + "/impact", json={"file_path": "service.py"})
    assert body.status_code == 200, body.text
    result = body.json()
    assert result["intent"] == "impact"
    assert "service.py" in result["evidence"] and "main.py" in result["evidence"]
    refs = result["retrieval"]["references"]
    assert refs[0]["path"] == "service.py" and refs[0]["coverage"] == "complete_file"
    assert any(ref["source"] == "graph" and ref["path"] == "main.py" for ref in refs)


def test_v1_without_chunks_remains_ready_and_explicitly_degraded(late_project, application, client):
    snapshot = application.state.persistence.snapshots[late_project.snapshot_id]
    store = application.state.artifacts
    bundle = store.read_ready(late_project.project_id, snapshot)
    manifest_path = bundle.index_path.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = 1
    for key in ("chunks_schema_version", "chunks_sha256", "chunk_count"):
        manifest.pop(key)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    # Only this test's freshly built temporary derivative is removed.
    bundle.chunks_path.unlink()
    response = client.post(late_project.url + "/ask", json={"question": "serve"})
    assert response.status_code == 200, response.text
    summary = response.json()["retrieval"]
    assert summary["status"] == "degraded" and "chunks_missing" in summary["diagnostics"]
    assert summary["references"][0]["chunk_ids"] == []
    assert summary["references"][0]["coverage"] == "complete_file"
    assert application.state.persistence.snapshots[snapshot.id].status == "ready"
    assert store.read_ready(late_project.project_id, snapshot).chunks_path is None


def test_agent_task_keeps_original_snapshot_after_current_pointer_changes(
    late_project, application, client, agent_worker, monkeypatch,
):
    p = late_project
    accepted = client.post("/api/v1/agent-runs", json={"project_id": str(p.project_id), "task": "serve"})
    assert accepted.status_code == 202, accepted.text
    task_id, run_id = UUID(accepted.json()["task_id"]), UUID(accepted.json()["run_id"])
    root = application.state.workspace.path_for(p.project_id)
    second = p.source.replace("original-snapshot-marker", "second-snapshot-marker")
    (root / "service.py").write_text(second, encoding="utf-8")
    uow = InMemoryUnitOfWork(application.state.persistence)
    with uow.transaction():
        new_snapshot = uow.snapshots.start(p.project_id, "b" * 40)
    bundle = application.state.artifacts.build_and_publish(
        p.project_id, new_snapshot.id, new_snapshot.commit_sha,
        lambda index, graph: application.state.adapter.index(
            root, index_path=index, graph_path=graph, project_id=p.project_id,
            snapshot_id=new_snapshot.id, commit_sha=new_snapshot.commit_sha,
        ), require_chunks=True,
    )
    with uow.transaction():
        uow.snapshots.mark_ready(new_snapshot.id, index_artifact_path=bundle.index_artifact_path,
                                 graph_artifact_path=bundle.graph_artifact_path, summary=bundle.summary)
        uow.projects.set_current_snapshot(p.project_id, new_snapshot.id)
    source = application.state.workspace.source
    def checkout(repository, commit, **kwargs):
        assert commit == "a" * 40
        (repository / "service.py").write_text(p.source, encoding="utf-8")
    monkeypatch.setattr(source, "checkout_exact", checkout)
    observed = []
    original = application.state.adapter.engine.run_agent
    def run(*args, **kwargs):
        context = kwargs["retrieval_context"]
        assert context.binding.snapshot_id == str(p.snapshot_id)
        assert context.binding.commit_sha == "a" * 40
        result = original(*args, **kwargs)
        observed.append(result)
        return result
    monkeypatch.setattr(application.state.adapter.engine, "run_agent", run)
    completed = agent_worker.execute(task_id)
    assert completed.status == AgentRunStatus.COMPLETED and completed.snapshot_id == p.snapshot_id
    assert application.state.persistence.projects[p.project_id].current_snapshot_id == new_snapshot.id
    retrieval = [r for r in observed[0].tool_results if r.tool == "retrieve_context"]
    assert retrieval and retrieval[0].ok
    assert "original-snapshot-marker" in retrieval[0].data["items"][0]["snippet"]
    assert "second-snapshot-marker" not in retrieval[0].data["items"][0]["snippet"]
    assert retrieval[0].data["retrieval"]["binding"]["snapshot_id"] == str(p.snapshot_id)
    assert agent_worker.execute(task_id).id == run_id
    assert len(observed) == 1  # repeated delivery does not enter Core again
    history = client.get(f"/api/v1/agent-runs/{run_id}/trace")
    assert history.status_code == 200, history.text
    summaries = [e["payload"]["retrieval"] for e in history.json()["events"] if "retrieval" in e["payload"]]
    assert summaries and all(s["binding"]["snapshot_id"] == str(p.snapshot_id) for s in summaries)


def test_unauthorized_call_never_prepares_evidence(late_project, application, client, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("authorization must precede Artifact reads and retrieval construction")
    monkeypatch.setattr(application.state.artifacts, "read_chunks", forbidden)
    monkeypatch.setattr(application.state.artifacts, "read_ready", forbidden)
    monkeypatch.setattr(application.state.artifacts, "validate", forbidden)
    monkeypatch.setattr(application.state.adapter, "snapshot_retrieval", forbidden)
    credentials = {"account_name": "other-a02a", "password": "isolated-a02a-test-password"}
    assert client.post("/api/v1/auth/register", json=credentials).status_code == 201
    token = client.post("/api/v1/auth/login", json=credentials).json()["access_token"]
    client.headers["Authorization"] = "Bearer " + token
    assert client.post(late_project.url + "/ask", json={"question": "serve"}).status_code == 404
    assert client.post(late_project.url + "/impact", json={"file_path": "service.py"}).status_code == 404
    assert client.post("/api/v1/agent-runs", json={"project_id": str(late_project.project_id), "task": "serve"}).status_code == 404


def test_realtime_and_final_trace_share_safe_retrieval_projection(late_project, application, agent_worker):
    p = late_project
    snapshot = application.state.persistence.snapshots[p.snapshot_id]
    projects = agent_worker.service().projects
    bundle = application.state.artifacts.read_ready(p.project_id, snapshot)
    root = application.state.workspace.path_for(p.project_id)
    context = projects.retrieval_context(root, snapshot, bundle)
    result = retrieve_context(ToolContext(root, retrieval_context=context), "serve")
    trace = ToolTrace(1, "retrieve_context", datetime.now(timezone.utc))
    trace.finish(result)
    # Unknown keys and source text must never reach public Trace.
    trace.retrieval = {**trace.retrieval, "api_key": "secret", "snippet": p.source}
    public = ApplicationTraceNormalizer._tool_finished(trace)
    event = ApplicationTraceEvent(0, *public)
    summary = event.payload["retrieval"]
    assert summary["binding"]["snapshot_id"] == str(snapshot.id)
    assert summary["references"][0]["start_line"] == 101
    assert "original-snapshot-marker" not in json.dumps(summary)
    assert "api_key" not in summary and "snippet" not in summary
    assert public_retrieval_summary(summary) == summary
    with projects.uow.transaction():
        run = projects.uow.agent_runs.create(p.project_id, p.snapshot_id, task="serve", run_type="agent", model=None)
    live = RealtimeTraceSession(replace(run, started_at=datetime.now(timezone.utc)),
                                SimpleNamespace(ensure_owned=lambda: None), None)
    live.observe("tool_finished", trace)
    assert live.events[-1].payload["retrieval"] == summary
