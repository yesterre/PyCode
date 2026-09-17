from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

from backend.app.repositories import InMemoryUnitOfWork
from backend.app.core.errors import BackendError


def _create(client, name):
    response = client.post("/api/v1/projects", json={
        "name": name, "repo_url": "https://github.com/example/demo.git",
    })
    assert response.status_code == 201
    return response.json()


def test_projects_page_empty_order_and_snapshot_field(client, persistence):
    assert client.get("/api/v1/projects").json() == {"projects": [], "has_more": False}
    projects = [_create(client, name) for name in ("A", "B", "C")]
    with InMemoryUnitOfWork(persistence).transaction():
        anchor = persistence.projects[UUID(projects[0]["id"])].updated_at
        for project in projects:
            key = UUID(project["id"])
            persistence.projects[key] = replace(persistence.projects[key], updated_at=anchor)
    ordered = sorted((p["id"] for p in projects), reverse=True)
    first = client.get("/api/v1/projects", params={"limit": 2}).json()
    assert [p["id"] for p in first["projects"]] == ordered[:2]
    assert first["has_more"] is True
    assert all(p["current_snapshot_id"] is None for p in first["projects"])
    second = client.get("/api/v1/projects", params={"limit": 2, "offset": 2}).json()
    assert [p["id"] for p in second["projects"]] == ordered[2:]
    assert second["has_more"] is False
    for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
        assert client.get("/api/v1/projects", params=params).status_code == 422


def test_snapshot_list_detail_ownership_and_nullable_counts(client, persistence):
    first, other = _create(client, "First"), _create(client, "Other")
    first_id, other_id = UUID(first["id"]), UUID(other["id"])
    uow = InMemoryUnitOfWork(persistence)
    with uow.transaction():
        older = uow.snapshots.start(first_id, "a" * 40)
        newer = uow.snapshots.start(first_id, "b" * 40)
        foreign = uow.snapshots.start(other_id, "c" * 40)
        persistence.snapshots[older.id] = replace(older, created_at=newer.created_at - timedelta(days=1))
    base = f"/api/v1/projects/{first_id}/snapshots"
    listed = client.get(base, params={"limit": 1}).json()["snapshots"]
    assert [item["id"] for item in listed] == [str(newer.id)]
    detail = client.get(f"{base}/{older.id}").json()
    assert detail["file_count"] is None and detail["status"] == "indexing"
    assert set(detail) == {"id", "project_id", "commit_sha", "status", "file_count",
                           "node_count", "edge_count", "created_at", "updated_at", "error_message"}
    assert client.get(f"{base}/{foreign.id}").status_code == 404
    assert client.get(f"{base}/{uuid4()}").status_code == 404
    assert client.get(f"/api/v1/projects/{uuid4()}/snapshots").status_code == 404
    assert client.get(base, params={"limit": 0}).status_code == 422


def test_graph_search_limits_and_errors(client, application, index_worker, project_url):
    accepted = index_worker.submit(client, project_url)
    index_worker.execute(UUID(accepted.json()["task_id"]))
    project = client.get(project_url).json()
    snapshot_id = project["current_snapshot_id"]
    assert snapshot_id and project["index_summary"]["file_count"] == 2
    path = f"{project_url}/snapshots/{snapshot_id}/graph"
    assert client.get(path).status_code == 422
    assert client.get(path, params={"q": "main", "file_path": "main.py"}).status_code == 422
    assert client.get(path, params={"file_path": "../main.py"}).status_code == 422
    assert client.get(path, params={"file_path": "C:\\secret.py"}).status_code == 422
    found = client.get(path, params={"q": "main", "limit": 2})
    assert found.status_code == 200, found.text
    body = found.json()
    assert body["snapshot_id"] == snapshot_id
    assert len(body["nodes"]) <= 2 and len(body["edges"]) <= 4
    assert all(edge["source"] in {n["id"] for n in body["nodes"]}
               and edge["target"] in {n["id"] for n in body["nodes"]}
               for edge in body["edges"])
    by_path = client.get(path, params={"file_path": "main.py"}).json()
    assert any(node["path"] == "main.py" for node in by_path["nodes"])
    assert client.get(path, params={"q": "nothing-matches"}).json() == {
        "snapshot_id": snapshot_id, "nodes": [], "edges": [], "truncated": False,
    }
    assert client.get(path, params={"q": "main", "limit": 201}).status_code == 422
    assert client.get(path, params={"q": "main", "node_type": "wrong"}).status_code == 422
    assert client.get(f"{project_url}/snapshots/{uuid4()}/graph", params={"q": "main"}).status_code == 404
    assert client.get(f"{project_url}/snapshots/{snapshot_id}").json()["commit_sha"] == "a" * 40
    assert client.get(project_url).json()["current_snapshot_id"] == snapshot_id
    other = _create(client, "Other")
    foreign = client.get(f"/api/v1/projects/{other['id']}/snapshots/{snapshot_id}/graph",
                         params={"q": "main"})
    assert foreign.status_code == 404
    assert "belongs" not in foreign.text


def test_graph_requires_ready_snapshot_and_rejects_unsafe_node_path(client, persistence):
    project = _create(client, "Unindexed")
    project_id = UUID(project["id"])
    uow = InMemoryUnitOfWork(persistence)
    with uow.transaction():
        snapshot = uow.snapshots.start(project_id, "a" * 40)
    response = client.get(f"/api/v1/projects/{project_id}/snapshots/{snapshot.id}/graph",
                          params={"q": "main"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "project_not_ready"
    from pycode.models import CodeGraph, GraphNode
    from backend.app.integrations.graph import local_graph
    try:
        local_graph(CodeGraph("C:/private", [GraphNode("bad", "file", "bad", "C:/secret.py")], []),
                    snapshot.id, q="bad", node_type=None, file_path=None, limit=100)
    except BackendError as error:
        assert error.code == "artifacts_unavailable"
        assert "C:/secret" not in error.message
    else:
        raise AssertionError("Unsafe graph node path was accepted")


def test_graph_projection_external_and_truncation():
    from pycode.models import CodeGraph, GraphEdge, GraphNode
    from backend.app.integrations.graph import local_graph

    graph = CodeGraph("C:/private", [
        GraphNode("file:a.py", "file", "a.py", "a.py"),
        GraphNode("func:a.py:f", "function", "f", "a.py"),
        GraphNode("external:os", "external", "os", None),
        GraphNode("func:print", "function", "print", None),
    ], [GraphEdge("file:a.py", "func:a.py:f", "contains"),
        GraphEdge("file:a.py", "external:os", "imports"),
        GraphEdge("func:a.py:f", "func:print", "calls")])
    result = local_graph(graph, uuid4(), q="a.py", node_type="file", file_path=None, limit=3)
    assert result["truncated"] is False
    assert any(node["type"] == "external" and node["path"] is None
               for node in result["nodes"])
    assert {edge["type"] for edge in result["edges"]} == {"contains", "imports"}
    assert len(result["nodes"]) == 3
    assert local_graph(graph, uuid4(), q="a.py", node_type="file",
                       file_path=None, limit=2)["truncated"] is True
    complete = local_graph(graph, uuid4(), q="a.py", node_type=None,
                           file_path=None, limit=10)
    assert {edge["type"] for edge in complete["edges"]} == {"contains", "imports", "calls"}
    assert complete["truncated"] is False
