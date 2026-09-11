from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.core.errors import BackendError
from backend.app.core.models import IndexSummary, ProjectStatus
from backend.app.repositories import InMemoryPersistence, InMemoryUnitOfWork
from pycode.storage import load_graph, load_index


def test_health_and_openapi_without_model_or_project():
    with TestClient(create_app()) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/docs").status_code == 200
        paths = client.get("/openapi.json").json()["paths"]
        assert set(paths) == {
            "/health", "/api/v1/projects", "/api/v1/projects/{project_id}",
            "/api/v1/projects/{project_id}/index", "/api/v1/projects/{project_id}/ask",
            "/api/v1/projects/{project_id}/impact",
        }


def test_production_project_api_requires_database_configuration(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/projects",
            json={"name": "Example", "repo_url": "https://github.com/example/demo.git"},
        )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "database_unavailable"


def test_project_registration_has_no_clone_and_allows_duplicate_source(client, application, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    body = {"name": "  Example  ", "repo_url": " https://github.com/example/demo.git ", "branch": " feature/demo "}
    response = client.post("/api/v1/projects", json=body)
    assert response.status_code == 201
    project = response.json()
    assert project["name"] == "Example"
    assert project["repo_url"] == body["repo_url"].strip()
    assert project["branch"] == "feature/demo"
    assert "local_path" not in project and "workspace_path" not in project
    assert project["status"] == "created"
    assert project["index_summary"] is None and project["last_error"] is None
    assert datetime.fromisoformat(project["created_at"]).utcoffset().total_seconds() == 0
    assert project["created_at"] == project["updated_at"]
    assert client.get("/api/v1/projects/" + project["id"]).json() == project
    duplicate = client.post("/api/v1/projects", json=body)
    assert duplicate.status_code == 201 and duplicate.json()["id"] != project["id"]
    assert application.state.workspace.source.calls == []
    assert not application.state.workspace.root.exists()


@pytest.mark.parametrize("body", [
    {}, {"name": "", "repo_url": "."}, {"name": "x", "repo_url": "  "},
    {"name": "  ", "repo_url": "."}, {"name": 123, "repo_url": "."},
    {"name": "x", "repo_url": ".", "extra": "unsupported"},
    {"name": "x", "repo_url": "https://github.com/a/b", "branch": " "},
    {"name": "x", "local_path": "."},
])
def test_invalid_registration_body(client, body):
    response = client.post("/api/v1/projects", json=body)
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


@pytest.mark.parametrize("path,status", [("missing", 422), ("repository/main.py", 422), ("bad\x00path", 422)])
def test_bad_repository_paths(client, path, status):
    response = client.post("/api/v1/projects", json={"name": "x", "repo_url": path})
    assert response.status_code == status


@pytest.mark.parametrize("project_id,status", [("invalid-uuid", 422), (str(uuid4()), 404)])
def test_unknown_project_and_invalid_uuid(client, project_id, status):
    url = "/api/v1/projects/" + project_id
    assert client.get(url).status_code == status
    assert client.post(url + "/index").status_code == status
    assert client.post(url + "/ask", json={"question": "entry"}).status_code == status
    assert client.post(url + "/impact", json={"file_path": "main.py"}).status_code == status


def test_index_and_analysis_http_chain(client, project_url, repository, llm):
    index = client.post(project_url + "/index")
    assert index.status_code == 200
    project = index.json()
    assert project["status"] == "ready" and project["last_error"] is None
    artifact_dir = next((repository.parent / "artifacts").iterdir())
    graph = load_graph(artifact_dir / "code_graph.json")
    assert len(load_index(artifact_dir / "index.json").files) == 2
    assert project["index_summary"] == {"file_count": 2, "node_count": len(graph.nodes), "edge_count": len(graph.edges)}
    assert client.get(project_url).json() == project
    for suffix, body, intent in [
        ("ask", {"question": "  entry main  ", "model": " test-model "}, "entry"),
        ("impact", {"file_path": "service.py"}, "impact"),
    ]:
        response = client.post(project_url + "/" + suffix, json=body)
        assert response.status_code == 200, response.text
        result = response.json()
        assert set(result) == {"project_id", "answer", "intent", "evidence"}
        assert result["project_id"] == project["id"] and result["intent"] == intent
        assert result["answer"] == "Offline HTTP answer" and "main.py" in result["evidence"]
    assert len(llm.prompts) == 2
    assert client.get(project_url).json()["status"] == "ready"


def test_snapshot_and_agent_runs_use_persistence(client, project_url, application):
    first = client.post(project_url + "/index")
    second = client.post(project_url + "/index")
    assert first.status_code == second.status_code == 200
    assert client.post(project_url + "/ask", json={"question": "entry"}).status_code == 200
    assert client.post(project_url + "/impact", json={"file_path": "main.py"}).status_code == 200

    unit_of_work = InMemoryUnitOfWork(application.state.persistence)
    project_id = UUID(project_url.rsplit("/", 1)[-1])
    with unit_of_work.transaction():
        snapshots = unit_of_work.snapshots.list_for_project(project_id)
        runs = unit_of_work.agent_runs.list_for_project(project_id)
    assert len(snapshots) == 1
    assert snapshots[0].commit_sha == "a" * 40
    assert snapshots[0].file_count == first.json()["index_summary"]["file_count"]
    assert {run.run_type for run in runs} == {"ask", "impact"}
    assert all(run.status == "completed" and run.snapshot_id == snapshots[0].id for run in runs)
    assert all(run.prompt_tokens is None and run.completion_tokens is None for run in runs)


def test_memory_repository_rejects_current_snapshot_from_another_project(persistence):
    unit_of_work = InMemoryUnitOfWork(persistence)
    with unit_of_work.transaction():
        first = unit_of_work.projects.create(
            "First", "https://github.com/example/first.git",
        )
        second = unit_of_work.projects.create(
            "Second", "https://github.com/example/second.git",
        )
        first_snapshot = unit_of_work.snapshots.upsert(
            first.id, "1" * 40,
            index_artifact_path="artifacts/first/index.json",
            graph_artifact_path="artifacts/first/code_graph.json",
            summary=IndexSummary(1, 2, 1),
        )
        second_snapshot = unit_of_work.snapshots.upsert(
            second.id, "2" * 40,
            index_artifact_path="artifacts/second/index.json",
            graph_artifact_path="artifacts/second/code_graph.json",
            summary=IndexSummary(2, 3, 2),
        )
        unit_of_work.projects.set_current_snapshot(first.id, first_snapshot.id)

    with pytest.raises(BackendError) as failure:
        with unit_of_work.transaction():
            unit_of_work.projects.set_current_snapshot(first.id, second_snapshot.id)
    assert failure.value.code == "database_conflict"

    with unit_of_work.transaction():
        persisted = unit_of_work.projects.get(first.id)
    assert persisted.current_snapshot_id == first_snapshot.id


@pytest.mark.parametrize("suffix,body", [
    ("ask", {}), ("ask", {"question": " "}), ("ask", {"question": "entry", "model": " "}),
    ("ask", {"question": "entry", "api_key": "not-accepted"}),
    ("impact", {"file_path": " "}), ("impact", {"file_path": "main.py", "model": ""}),
    ("impact", {"file_path": "main.py", "unknown": True}),
])
def test_analysis_validation(client, ready_url, llm, suffix, body):
    response = client.post(ready_url + "/" + suffix, json=body)
    assert response.status_code == 422 and isinstance(response.json()["detail"], list)
    assert llm.prompts == []


def test_not_ready_requires_current_session_index(client, project_url, repository, llm, application):
    from pycode import PyCodeEngine

    unit_of_work = InMemoryUnitOfWork(application.state.persistence)
    project_id = UUID(project_url.rsplit("/", 1)[-1])
    with unit_of_work.transaction():
        project = unit_of_work.projects.set_status(
            project_id, ProjectStatus.CREATED,
            workspace_path=application.state.workspace.persistent_path_for(project_id),
        )
    application.state.workspace.prepare(project)
    PyCodeEngine().index(repository)
    PyCodeEngine().graph(repository)
    for suffix, body in [("ask", {"question": "entry"}), ("impact", {"file_path": "main.py"})]:
        response = client.post(project_url + "/" + suffix, json=body)
        assert response.status_code == 409 and response.json()["detail"]["code"] == "project_not_ready"
    assert llm.prompts == []


@pytest.mark.parametrize("bad_content", [b"def broken(:\n", b"\xff\xfe\xfa"])
def test_source_error_and_reindex_recovery(client, ready_url, repository, bad_content):
    next((repository.parent / "artifacts").glob("*/index.json")).unlink()
    file = repository / "broken.py"
    file.write_bytes(bad_content)
    response = client.post(ready_url + "/index")
    assert response.status_code == 422 and response.json()["detail"]["code"] == "invalid_source"
    failed = client.get(ready_url).json()
    assert failed["status"] == "failed" and failed["index_summary"] is None and failed["last_error"]
    assert client.post(ready_url + "/ask", json={"question": "entry"}).status_code == 409
    file.write_text("value = 1\n", encoding="utf-8")
    ready = client.post(ready_url + "/index").json()
    assert ready["status"] == "ready" and ready["last_error"] is None
    assert ready["index_summary"]["file_count"] == 3
    assert ready["created_at"] == failed["created_at"]


@pytest.mark.parametrize("artifact", ["index.json", "code_graph.json"])
@pytest.mark.parametrize("damage", ["missing", "json", "shape"])
def test_unavailable_artifacts_fail_before_model_and_can_recover(client, ready_url, repository, llm, artifact, damage):
    path = next((repository.parent / "artifacts").glob(f"*/{artifact}"))
    if damage == "missing":
        path.unlink()
    else:
        path.write_text("not json" if damage == "json" else "{}", encoding="utf-8")
    response = client.post(ready_url + "/ask", json={"question": "entry"})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "artifacts_unavailable"
    assert llm.prompts == []
    failed = client.get(ready_url).json()
    assert failed["status"] == "failed" and failed["index_summary"] is None
    assert client.post(ready_url + "/index").status_code == 200
    assert client.post(ready_url + "/impact", json={"file_path": "main.py"}).status_code == 200


@pytest.mark.parametrize("target,status", [
    ("../outside.py", 403), ("/absolute.py", 422), ("C:\\absolute.py", 422),
    ("C:relative.py", 422), ("\\rooted.py", 422), ("bad\x00.py", 422),
    ("missing.py", 404), ("main.txt", 422), ("directory.py", 422),
])
def test_impact_path_boundary(client, ready_url, repository, llm, target, status):
    (repository / "directory.py").mkdir()
    response = client.post(ready_url + "/impact", json={"file_path": target})
    assert response.status_code == status, response.text
    assert llm.prompts == []


def test_impact_normalizes_separators(client, ready_url, repository):
    response = client.post(ready_url + "/impact", json={"file_path": ".\\service.py"})
    assert response.status_code == 200
    assert "service.py" in response.json()["evidence"]


def test_new_app_has_no_registration_and_keeps_artifacts(client, ready_url, repository, adapter, tmp_path):
    with TestClient(create_app(adapter=adapter, persistence=InMemoryPersistence())) as other:
        assert other.get(ready_url).status_code == 404
        response = other.post("/api/v1/projects", json={"name": "New", "repo_url": "https://github.com/example/demo.git"})
        assert response.status_code == 201 and response.json()["status"] == "created"
        assert response.json()["id"] != ready_url.rsplit("/", 1)[-1]
    assert client.get(ready_url).json()["status"] == "ready"
    assert next((repository.parent / "artifacts").glob("*/index.json")).exists()


def test_published_bundle_is_reused_after_database_activation_failure(
    client, project_url, repository, adapter, monkeypatch,
):
    from backend.app.repositories.memory import InMemorySnapshotRepository

    original = InMemorySnapshotRepository.mark_ready
    failed = False

    def fail_once(self, *args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise BackendError("database_failed", "A database transaction failed.")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(InMemorySnapshotRepository, "mark_ready", fail_once)
    first = client.post(project_url + "/index")
    assert first.status_code == 500
    artifact = next((repository.parent / "artifacts").glob("*/manifest.json"))
    assert artifact.exists()

    forbidden = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("published artifacts must be recovered without rebuilding")
    )
    monkeypatch.setattr(adapter.engine, "index", forbidden)
    monkeypatch.setattr(adapter.engine, "graph", forbidden)
    recovered = client.post(project_url + "/index")
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["status"] == "ready"


def test_phase3_current_artifact_is_archived_without_rewriting_other_history(
    client, project_url, application, adapter, repository, monkeypatch,
):
    project_id = UUID(project_url.rsplit("/", 1)[-1])
    unit_of_work = InMemoryUnitOfWork(application.state.persistence)
    with unit_of_work.transaction():
        project = unit_of_work.projects.set_status(
            project_id, ProjectStatus.READY,
            workspace_path=application.state.workspace.persistent_path_for(project_id),
        )
    application.state.workspace.prepare(project)
    adapter.engine.index(repository)
    adapter.engine.graph(repository)
    legacy_summary = IndexSummary(
        len(load_index(repository / ".pclens/index.json").files),
        len(load_graph(repository / ".pclens/code_graph.json").nodes),
        len(load_graph(repository / ".pclens/code_graph.json").edges),
    )
    with unit_of_work.transaction():
        current = unit_of_work.snapshots.upsert(
            project_id, "a" * 40,
            index_artifact_path=".pclens/index.json",
            graph_artifact_path=".pclens/code_graph.json",
            summary=legacy_summary,
        )
        historical = unit_of_work.snapshots.upsert(
            project_id, "b" * 40,
            index_artifact_path=".pclens/index.json",
            graph_artifact_path=".pclens/code_graph.json",
            summary=IndexSummary(9, 9, 9),
        )
        unit_of_work.projects.set_current_snapshot(project_id, current.id)

    forbidden = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("legacy relocation must not rebuild a valid current Snapshot")
    )
    monkeypatch.setattr(adapter.engine, "index", forbidden)
    monkeypatch.setattr(adapter.engine, "graph", forbidden)
    response = client.post(project_url + "/index")
    assert response.status_code == 200, response.text

    with unit_of_work.transaction():
        current = unit_of_work.snapshots.get(current.id)
        historical = unit_of_work.snapshots.get(historical.id)
    assert current.index_artifact_path.startswith("artifacts/")
    assert current.status == "ready"
    assert historical.index_artifact_path == ".pclens/index.json"
    assert historical.status == "ready" and historical.file_count == 9
