"""Real local Git transport tests; production continues to reject file URLs."""

import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from backend.app.core.errors import BackendError
from backend.app.infrastructure.repositories import (
    GIT_LOGGER, GitRepositorySource, RepositoryWorkspace,
)
from backend.app.main import create_app
from backend.app.repositories import InMemoryPersistence
from pycode.storage import load_graph, load_index


URL = "https://github.com/example/public.git"


def git(root, *args, input=None):
    return subprocess.run(
        ["git", "-c", "user.name=Offline Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "core.hooksPath=" + os.devnull, *args],
        cwd=root, input=input, capture_output=True, check=True, timeout=15,
    ).stdout


@pytest.fixture
def remote(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("Git executable is required for offline repository ingestion tests")
    root = tmp_path / "remote"
    root.mkdir()
    git(root, "init", "-b", "default-branch")
    (root / "main.py").write_text("def main():\n    return 42\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    return root


@pytest.fixture
def offline_git(remote, monkeypatch):
    original = subprocess.run
    calls = []

    def run(command, **kwargs):
        if kwargs.get("env", {}).get("GIT_ALLOW_PROTOCOL") == "https":
            calls.append((list(command), dict(kwargs)))
            # Only this test transport substitutes a trusted temporary repository.
            # All actual clone/fetch/tree/checkout work still uses production code.
            command = list(command)
            if URL in command:
                command[command.index(URL)] = remote.as_uri()
                command[1:1] = ["-c", "protocol.file.allow=always"]
                kwargs["env"] = {**kwargs["env"], "GIT_ALLOW_PROTOCOL": "file"}
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


@pytest.fixture
def git_app(tmp_path, adapter, offline_git):
    return create_app(
        adapter=adapter, workspace=RepositoryWorkspace(tmp_path / "managed"),
        persistence=InMemoryPersistence(),
    )


def register(client, branch=None):
    response = client.post("/api/v1/projects", json={"name": "../untrusted name", "repo_url": URL, "branch": branch})
    assert response.status_code == 201, response.text
    return "/api/v1/projects/" + response.json()["id"]


def test_real_git_http_chain_fetches_new_commit_and_preserves_snapshots(
    git_app, offline_git, remote,
):
    with TestClient(git_app) as client:
        url = register(client)
        assert offline_git == [] and not git_app.state.workspace.root.exists()
        response = client.post(url + "/index")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "ready"
        root = git_app.state.workspace.path_for(UUID(response.json()["id"]))
        assert root == git_app.state.workspace.root / response.json()["id"] / "repo"
        first_artifact = next((root.parent / "artifacts").iterdir())
        assert len(load_index(first_artifact / "index.json").files) == 1
        assert load_graph(first_artifact / "code_graph.json").nodes
        for operation, body in [("ask", {"question": "entry main"}), ("impact", {"file_path": "main.py"})]:
            answer = client.post(url + "/" + operation, json=body)
            assert answer.status_code == 200, answer.text
            assert answer.json()["answer"] == "Offline HTTP answer"
            assert "main.py" in answer.json()["evidence"]
        (remote / "later.py").write_text("later = True\n", encoding="utf-8")
        git(remote, "add", ".")
        git(remote, "commit", "-m", "remote update")
        (root / "local.py").write_text("local = True\n", encoding="utf-8")
        clone_count = sum("clone" in command for command, _ in offline_git)
        assert client.post(url + "/index").json()["index_summary"]["file_count"] == 2
        assert sum("clone" in command for command, _ in offline_git) == clone_count
        assert (root / "later.py").exists()
        assert not (root / "local.py").exists()
        artifact_dirs = [path for path in (root.parent / "artifacts").iterdir() if path.is_dir()]
        assert len(artifact_dirs) == 2
        assert sorted(len(load_index(path / "index.json").files) for path in artifact_dirs) == [1, 2]
        other = register(client)
        assert client.post(other + "/index").status_code == 200
        other_root = git_app.state.workspace.path_for(UUID(other.rsplit("/", 1)[-1]))
        assert other_root != root and (other_root / "later.py").exists()
        assert not (other_root / "local.py").exists()


def test_explicit_branch_and_missing_branch_retry(git_app, remote):
    with TestClient(git_app) as client:
        url = register(client, "feature/demo")
        response = client.post(url + "/index")
        assert response.status_code == 422
        failed = client.get(url).json()
        assert failed["status"] == "failed" and failed["last_error"]
        root = git_app.state.workspace.path_for(UUID(failed["id"]))
        assert not root.exists() and list(root.parent.iterdir()) == []
        git(remote, "checkout", "-b", "feature/demo")
        (remote / "feature.py").write_text("enabled = True\n", encoding="utf-8")
        git(remote, "add", ".")
        git(remote, "commit", "-m", "feature")
        result = client.post(url + "/index")
        assert result.status_code == 200 and result.json()["last_error"] is None
        assert (root / "feature.py").exists()


@pytest.mark.parametrize("mode,name", [
    ("120000", "outside.py"), ("160000", "nested"), ("100644", ".pclens/index.json"),
])
def test_git_tree_rejects_symlink_submodule_and_remote_artifacts(git_app, remote, mode, name, offline_git):
    # Construct tree metadata directly: works even without OS symlink privileges.
    oid = (git(remote, "rev-parse", "HEAD") if mode == "160000" else
           git(remote, "hash-object", "-w", "--stdin", input=b"../../outside.py")).decode().strip()
    git(remote, "update-index", "--add", "--cacheinfo", f"{mode},{oid},{name}")
    git(remote, "commit", "-m", "unsafe tree")
    with TestClient(git_app) as client:
        url = register(client)
        response = client.post(url + "/index")
        assert response.status_code == 403, response.text
        assert response.json()["detail"]["code"] == "unsafe_repository"
        assert client.get(url).json()["status"] == "failed"
        assert not any("checkout" in command for command, _ in offline_git)
        root = git_app.state.workspace.path_for(UUID(url.rsplit("/", 1)[-1]))
        assert list(root.parent.iterdir()) == []


def test_repository_programs_and_inherited_git_commands_never_execute(git_app, remote, tmp_path, monkeypatch, offline_git):
    marker = tmp_path / "EXECUTED"
    script = f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
    for name in ("setup.py", "test_danger.py", "main.py"):
        (remote / name).write_text(script, encoding="utf-8")
    (remote / "package.json").write_text('{"scripts":{"preinstall":"exit 1"}}', encoding="utf-8")
    (remote / ".gitattributes").write_text("*.py filter=danger\n", encoding="utf-8")
    git(remote, "add", ".")
    git(remote, "commit", "-m", "untrusted programs")
    config = tmp_path / "evil.gitconfig"
    config.write_text('[filter "danger"]\n smudge = definitely-not-a-real-command\n required = true\n', encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "definitely-not-a-real-command")
    with TestClient(git_app) as client:
        url = register(client)
        response = client.post(url + "/index")
        assert response.status_code == 200, response.text
    assert not marker.exists()
    for command, kwargs in offline_git:
        assert command[0] == "git" and kwargs["shell"] is False
        assert kwargs["timeout"] == 120 and kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert "GIT_CONFIG_COUNT" not in kwargs["env"]
        assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
        assert "http.followRedirects=false" in command
        assert not any("recurse-submodules" in arg for arg in command)
    assert all(any(op in command for op in (
        "clone", "ls-remote", "ls-tree", "checkout", "clean", "rev-parse",
        "update-ref",
    ))
               for command, _ in offline_git)


@pytest.mark.parametrize("repo_url,status", [
    ("http://github.com/a/b", 422), ("ssh://github.com/a/b", 422),
    ("file:///tmp/repo", 422), ("git@github.com:a/b", 422),
    ("https://user:secret@github.com/a/b", 422), ("https://user@github.com/a/b", 422),
    ("https://github.com/a/b?token=secret", 422), ("https://github.com/a/b#main", 422),
    ("https://github.com:invalid/a/b", 422), ("https://github.com:444/a/b", 422),
    ("https://github.com/a/../b", 422), ("https://github.com/a/%2e%2e/b", 422),
    ("https://github.com/", 422),
    ("https://github.com\n/a/b", 422), ("https://github.com.evil.test/a/b", 403),
    ("https://127.0.0.1/a/b", 403), ("https://localhost/a/b", 403),
    ("https://gitlab.com/group/repo.git", 201), ("https://gitee.com/team/repo.git", 201),
])
def test_source_url_policy(client, application, repo_url, status):
    response = client.post("/api/v1/projects", json={"name": "x", "repo_url": repo_url})
    assert response.status_code == status, response.text
    assert application.state.workspace.source.calls == []


@pytest.mark.parametrize("branch", ["--upload-pack=bad", "../main", "main.lock", "a//b", "a@{b", "a\\b", "a b", "a:main"])
def test_invalid_branch_is_rejected_before_clone(client, branch):
    response = client.post("/api/v1/projects", json={"name": "x", "repo_url": URL, "branch": branch})
    assert response.status_code == 422


@pytest.mark.parametrize("failure,status,code", [
    (FileNotFoundError("private executable path"), 503, "git_unavailable"),
    (subprocess.TimeoutExpired(["git"], 120, stderr=b"private"), 504, "repository_timeout"),
    (subprocess.CalledProcessError(128, ["git"], stderr=b"private"), 502, "repository_failed"),
])
def test_git_errors_are_safe_and_staging_is_removed(tmp_path, adapter, monkeypatch, failure, status, code):
    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(subprocess, "run", fail)
    app = create_app(
        adapter=adapter, workspace=RepositoryWorkspace(tmp_path / "managed"),
        persistence=InMemoryPersistence(),
    )
    with TestClient(app) as client:
        url = register(client)
        response = client.post(url + "/index")
        assert response.status_code == status and response.json()["detail"]["code"] == code
        project = client.get(url).json()
        assert project["status"] == "failed" and project["index_summary"] is None
        assert project["last_error"] == response.json()["detail"]["message"]
        assert "private" not in response.text and "private" not in project["last_error"]
        root = app.state.workspace.path_for(UUID(project["id"]))
        assert list(root.parent.iterdir()) == []


def test_git_network_environment_is_allowlisted_and_failure_log_is_safe(
    tmp_path, monkeypatch,
):
    allowed = {
        "HTTPS_PROXY": "https://proxy-user:proxy-password@proxy.invalid:8443",
        "NO_PROXY": "localhost,127.0.0.1",
        "SSL_CERT_FILE": "C:/trusted/ca.pem",
        "GIT_SSL_CAINFO": "C:/trusted/git-ca.pem",
    }
    dangerous = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.sslVerify",
        "GIT_CONFIG_VALUE_0": "false",
        "GIT_PROXY_COMMAND": "unsafe-proxy-command",
        "GIT_SSL_NO_VERIFY": "true",
        "CURL_CA_BUNDLE": "C:/unapproved/curl-ca.pem",
    }
    for key, value in {**allowed, **dangerous}.items():
        monkeypatch.setenv(key, value)
    captured = {}
    logs = []
    project_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    def fail(command, **kwargs):
        captured.update(kwargs["env"])
        raise subprocess.CalledProcessError(
            128, command,
            stderr=(
                b"fatal: proxy https://proxy-user:proxy-password@proxy.invalid:8443 "
                b"token=top-secret\nAuthorization: Bearer bearer-secret\n" + b"x" * 3000
            ),
        )

    monkeypatch.setattr(subprocess, "run", fail)
    monkeypatch.setattr(
        GIT_LOGGER, "error",
        lambda message, *args: logs.append(message % args),
    )
    assert GIT_LOGGER.name == "uvicorn.error"
    destination_parent = tmp_path / "destination"
    destination_parent.mkdir()
    with pytest.raises(BackendError) as failure:
        GitRepositorySource().clone(
            URL, "main", destination_parent / "repo", project_id=project_id,
        )

    assert failure.value.code == "repository_failed"
    assert all(captured[key] == value for key, value in allowed.items())
    assert not dangerous.keys() & captured.keys()
    assert captured["GIT_CONFIG_NOSYSTEM"] == "1"
    assert captured["GIT_CONFIG_GLOBAL"] == os.devnull
    assert len(logs) == 1
    log = logs[0]
    assert str(project_id) in log
    assert "stage=resolve_remote_branch" in log
    assert "operation=ls-remote" in log
    assert "remote_host=github.com" in log and "branch=main" in log
    assert "returncode=128" in log and "timeout=120" in log
    assert "<redacted-url>" in log and "<truncated>" in log
    for secret in (
        *allowed.values(), "top-secret", "bearer-secret",
        "proxy-user", "proxy-password",
    ):
        assert secret not in log


def test_preparing_holds_lock_and_does_not_block_queries(client, project_url, application, monkeypatch):
    entered, release = Event(), Event()
    source = application.state.workspace.source
    original = source.clone

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(source, "clone", slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, project_url + "/index")
        try:
            assert entered.wait(5)
            assert client.get(project_url).json()["status"] == "preparing"
            assert client.get("/health").status_code == 200
            assert client.post(project_url + "/index").json()["detail"]["code"] == "project_busy"
            assert client.post(project_url + "/ask", json={"question": "entry"}).status_code == 409
        finally:
            release.set()
        assert future.result(timeout=5).status_code == 200


def test_workspace_parent_escape_is_denied_before_clone(client, project_url, repository, application, tmp_path, monkeypatch):
    outside = tmp_path / "external"
    outside.mkdir()
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        return outside if path == repository.parent else original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    assert client.post(project_url + "/index").status_code == 403
    assert application.state.workspace.source.calls == [] and list(outside.iterdir()) == []


def test_reindex_rejects_source_file_resolving_outside_workspace(client, ready_url, repository, tmp_path, monkeypatch):
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        return tmp_path / "secret.py" if path == repository / "main.py" else original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    assert client.post(ready_url + "/index").status_code == 403
    assert client.get(ready_url).json()["status"] == "failed"


def test_missing_ready_workspace_requires_reindex(client, ready_url, repository):
    repository.rename(repository.with_name("removed"))
    response = client.post(ready_url + "/ask", json={"question": "entry"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "workspace_unavailable"
    assert client.get(ready_url).json()["status"] == "failed"


def test_configurable_workspace_and_host_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_WORKSPACE_ROOT", str(tmp_path / "configured"))
    monkeypatch.setenv("PYCODE_GIT_ALLOWED_HOSTS", "gitlab.com")
    monkeypatch.setenv("PYCODE_GIT_TIMEOUT_SECONDS", "30")
    app = create_app(persistence=InMemoryPersistence())
    assert app.state.workspace.root == tmp_path / "configured"
    assert app.state.workspace.source.timeout == 30
    with TestClient(app) as client:
        assert client.post("/api/v1/projects", json={"name": "x", "repo_url": URL}).status_code == 403
        assert client.post("/api/v1/projects", json={"name": "x", "repo_url": "https://gitlab.com/a/b"}).status_code == 201
    assert not app.state.workspace.root.exists()
