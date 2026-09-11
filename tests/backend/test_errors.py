import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from pycode.llm_client import LLMError


@pytest.mark.parametrize("category,status,code", [
    ("missing_config", 503, "model_unavailable"),
    ("unsupported_api_type", 503, "model_unavailable"),
    ("dependency_missing", 503, "model_unavailable"),
    ("timeout", 504, "model_timeout"),
    ("auth_failed", 502, "model_failed"),
    ("rate_limited", 502, "model_failed"),
    ("unknown", 502, "model_failed"),
])
@pytest.mark.parametrize("operation,body", [("ask", {"question": "entry"}), ("impact", {"file_path": "main.py"})])
def test_model_errors_are_sanitized_and_do_not_change_ready(
    client, ready_url, llm, monkeypatch, caplog, category, status, code, operation, body,
):
    def fail(prompt):
        raise LLMError("upstream-private-detail", category=category)

    with monkeypatch.context() as patch:
        patch.setattr(llm, "generate", fail)
        with caplog.at_level(logging.ERROR, logger="pycode.backend"):
            response = client.post(ready_url + "/" + operation, json=body)
        assert response.status_code == status
        assert response.json()["detail"]["code"] == code
        assert "upstream-private-detail" not in response.text
        assert "upstream-private-detail" in caplog.text
        assert client.get(ready_url).json()["status"] == "ready"
    # Also proves the operation lock is released on model failure.
    assert client.post(ready_url + "/ask", json={"question": "entry"}).status_code == 200


def test_model_name_reaches_core_after_trimming(client, ready_url, adapter, monkeypatch):
    original = adapter.engine.ask
    seen = []

    def ask(*args, **kwargs):
        seen.append(kwargs["model"])
        return original(*args, **kwargs)

    monkeypatch.setattr(adapter.engine, "ask", ask)
    assert client.post(ready_url + "/ask", json={"question": "entry", "model": " demo-model "}).status_code == 200
    assert seen == ["demo-model"]


def test_graph_failure_marks_failed_and_releases_lock(
    client, ready_url, repository, adapter, monkeypatch,
):
    next((repository.parent / "artifacts").glob("*/code_graph.json")).unlink()

    def forbidden(root, *args, **kwargs):
        raise PermissionError("private filesystem detail")

    with monkeypatch.context() as patch:
        patch.setattr(adapter.engine, "graph", forbidden)
        response = client.post(ready_url + "/index")
        assert response.status_code == 403
        failed = client.get(ready_url).json()
        assert failed["status"] == "failed" and failed["index_summary"] is None
        assert "private filesystem detail" not in response.text and "private filesystem detail" not in failed["last_error"]
        assert client.post(ready_url + "/impact", json={"file_path": "main.py"}).status_code == 409
    assert client.post(ready_url + "/index").json()["status"] == "ready"


def test_unexpected_index_error_is_logged_and_sanitized(application, project_url, adapter, monkeypatch, caplog):
    def crash(root, *args, **kwargs):
        raise RuntimeError("internal implementation detail")

    monkeypatch.setattr(adapter.engine, "index", crash)
    with TestClient(application, raise_server_exceptions=False) as client:
        with caplog.at_level(logging.ERROR, logger="pycode.backend"):
            response = client.post(project_url + "/index")
        assert response.status_code == 500 and response.json()["detail"]["code"] == "internal_error"
        assert "internal implementation detail" not in response.text
        assert "internal implementation detail" in caplog.text
        assert client.get(project_url).json()["status"] == "failed"


def test_removed_repository_is_cloned_again(client, ready_url, repository, application):
    repository.rename(repository.with_name("moved"))
    assert client.post(ready_url + "/index").status_code == 200
    assert len(application.state.workspace.source.calls) == 2


def test_workspace_permission_error(client, ready_url, repository, monkeypatch):
    original = Path.stat

    def denied(path, *args, **kwargs):
        if path == repository:
            raise PermissionError("private detail")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)
    response = client.post(ready_url + "/index")
    assert response.status_code == 403 and "private detail" not in response.text
