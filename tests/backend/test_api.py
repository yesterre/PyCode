from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
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
    graph = load_graph(repository / ".pclens/code_graph.json")
    assert len(load_index(repository / ".pclens/index.json").files) == 2
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

    project = application.state.project_store.get(UUID(project_url.rsplit("/", 1)[-1]))
    application.state.workspace.prepare(project)
    PyCodeEngine().index(repository)
    PyCodeEngine().graph(repository)
    for suffix, body in [("ask", {"question": "entry"}), ("impact", {"file_path": "main.py"})]:
        response = client.post(project_url + "/" + suffix, json=body)
        assert response.status_code == 409 and response.json()["detail"]["code"] == "project_not_ready"
    assert llm.prompts == []


@pytest.mark.parametrize("bad_content", [b"def broken(:\n", b"\xff\xfe\xfa"])
def test_source_error_and_reindex_recovery(client, ready_url, repository, bad_content):
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
    path = repository / ".pclens" / artifact
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
    with TestClient(create_app(adapter=adapter)) as other:
        assert other.get(ready_url).status_code == 404
        response = other.post("/api/v1/projects", json={"name": "New", "repo_url": "https://github.com/example/demo.git"})
        assert response.status_code == 201 and response.json()["status"] == "created"
        assert response.json()["id"] != ready_url.rsplit("/", 1)[-1]
    assert client.get(ready_url).json()["status"] == "ready"
    assert (repository / ".pclens/index.json").exists()
