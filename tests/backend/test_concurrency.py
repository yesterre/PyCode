from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event


def test_index_lock_keeps_health_get_and_other_projects_responsive(
    client, ready_url, repository, adapter, tmp_path, monkeypatch, application,
):
    entered, release = Event(), Event()
    original = adapter.engine.graph

    def slow_graph(root):
        if root == repository:
            entered.set()
            assert release.wait(10), "test did not release indexing"
        return original(root)

    monkeypatch.setattr(adapter.engine, "graph", slow_graph)
    other_root = tmp_path / "other"
    other_root.mkdir()
    (other_root / "app.py").write_text("print('other')\n", encoding="utf-8")
    other_repo_url = "https://gitlab.com/example/other.git"
    application.state.workspace.source.repositories[other_repo_url] = other_root
    other = client.post("/api/v1/projects", json={"name": "Other", "repo_url": other_repo_url})
    other_url = "/api/v1/projects/" + other.json()["id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(client.post, ready_url + "/index")
        try:
            assert entered.wait(5)
            assert client.get("/health").status_code == 200
            project = client.get(ready_url).json()
            assert project["status"] == "indexing" and project["index_summary"] is None and project["last_error"] is None
            for suffix, kwargs in [("index", {}), ("ask", {"json": {"question": "entry"}}),
                                   ("impact", {"json": {"file_path": "main.py"}})]:
                response = client.post(ready_url + "/" + suffix, **kwargs)
                assert response.status_code == 409 and response.json()["detail"]["code"] == "project_busy"
            assert client.post(other_url + "/index").status_code == 200
            answer = client.post(other_url + "/ask", json={"question": "entry"}).json()
            assert "app.py" in answer["evidence"] and "main.py" not in answer["evidence"]
        finally:
            release.set()
        assert future.result(timeout=5).status_code == 200
    assert client.get(ready_url).json()["status"] == "ready"


def test_analysis_holds_project_lock_without_blocking_get(client, ready_url, llm, monkeypatch):
    entered, release = Event(), Event()

    def slow_model(prompt):
        entered.set()
        assert release.wait(10)
        return "completed"

    monkeypatch.setattr(llm, "generate", slow_model)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, ready_url + "/ask", json={"question": "entry"})
        try:
            assert entered.wait(5)
            assert client.get(ready_url).json()["status"] == "ready"
            assert client.get("/health").status_code == 200
            assert client.post(ready_url + "/index").json()["detail"]["code"] == "project_busy"
            assert client.post(ready_url + "/impact", json={"file_path": "main.py"}).status_code == 409
        finally:
            release.set()
        assert future.result(timeout=5).json()["answer"] == "completed"
    assert client.post(ready_url + "/index").status_code == 200


def test_concurrent_same_source_registration_creates_independent_projects(client):
    barrier = Barrier(2)

    def create():
        barrier.wait(timeout=5)
        return client.post("/api/v1/projects", json={"name": "same", "repo_url": "https://github.com/example/demo.git"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: create(), range(2)))
    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json()["id"] != responses[1].json()["id"]
