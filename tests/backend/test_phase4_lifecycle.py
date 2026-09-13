"""Phase 4 lifecycle tests using a local bare repository as the remote."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from backend.app.core.errors import BackendError
from backend.app.infrastructure.repositories import GitRepositorySource, RepositoryWorkspace
from backend.app.main import create_app
from backend.app.repositories import InMemoryPersistence
from pycode.storage import load_index


URL = "https://github.com/example/phase4.git"


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        [
            "git", "-c", "user.name=Offline Test",
            "-c", "user.email=test@example.invalid",
            "-c", "commit.gpgsign=false",
            "-c", "core.hooksPath=" + os.devnull,
            *args,
        ],
        cwd=root, capture_output=True, check=True, timeout=15,
    ).stdout


@dataclass(frozen=True)
class BareRemote:
    source: Path
    bare: Path
    first_commit: str

    def commit_and_push(self, name: str, content: str, message: str) -> str:
        (self.source / name).write_text(content, encoding="utf-8")
        git(self.source, "add", ".")
        git(self.source, "commit", "-m", message)
        git(self.source, "push", "origin", "main")
        return git(self.source, "rev-parse", "HEAD").decode("ascii").strip()


@pytest.fixture
def bare_remote(tmp_path: Path) -> BareRemote:
    if shutil.which("git") is None:
        pytest.skip("Git executable is required for offline lifecycle tests")
    bare = tmp_path / "remote.git"
    source = tmp_path / "source"
    bare.mkdir()
    source.mkdir()
    git(bare, "init", "--bare", "-b", "main")
    git(source, "init", "-b", "main")
    (source / "main.py").write_text(
        "def main():\n    return 1\n", encoding="utf-8",
    )
    git(source, "add", ".")
    git(source, "commit", "-m", "first")
    first_commit = git(source, "rev-parse", "HEAD").decode("ascii").strip()
    git(source, "remote", "add", "origin", bare.as_uri())
    git(source, "push", "-u", "origin", "main")
    return BareRemote(source, bare, first_commit)


@pytest.fixture
def bare_transport(bare_remote: BareRemote, monkeypatch):
    original = subprocess.run
    calls: list[list[str]] = []

    def run(command, **kwargs):
        if kwargs.get("env", {}).get("GIT_ALLOW_PROTOCOL") == "https":
            calls.append(list(command))
            command = list(command)
            if URL in command:
                command[command.index(URL)] = bare_remote.bare.as_uri()
                command[1:1] = ["-c", "protocol.file.allow=always"]
                kwargs["env"] = {**kwargs["env"], "GIT_ALLOW_PROTOCOL": "file"}
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def register(client: TestClient) -> tuple[str, UUID]:
    response = client.post(
        "/api/v1/projects",
        json={"name": "Lifecycle", "repo_url": URL},
    )
    assert response.status_code == 201, response.text
    project_id = UUID(response.json()["id"])
    return f"/api/v1/projects/{project_id}", project_id


def test_bare_remote_history_failure_fallback_idempotency_and_restart(
    tmp_path, bare_remote, bare_transport, adapter, monkeypatch,
    index_worker_factory, task_dispatcher_factory,
):
    state = InMemoryPersistence()
    workspace_root = tmp_path / "managed"
    app = create_app(
        adapter=adapter,
        workspace=RepositoryWorkspace(workspace_root),
        persistence=state,
        task_dispatcher=task_dispatcher_factory(),
    )
    worker = index_worker_factory(app)
    with TestClient(app) as client:
        url, project_id = register(client)
        first = worker.run(client, url)
        assert first.status_code == 200, first.text
        first_snapshot = next(iter(state.snapshots.values()))
        assert first_snapshot.commit_sha == bare_remote.first_commit
        assert state.projects[project_id].current_snapshot_id == first_snapshot.id
        first_index = (
            workspace_root / str(project_id) / first_snapshot.index_artifact_path
        )
        first_bytes = first_index.read_bytes()

        # A refresh failure is a failed update attempt, not invalidation of A.
        source = app.state.workspace.source
        with monkeypatch.context() as patch:
            patch.setattr(
                source, "update",
                lambda *args, **kwargs: (_ for _ in ()).throw(BackendError(
                    "repository_failed", "Repository preparation failed.",
                )),
            )
            failed_fetch = worker.submit(client, url)
            with pytest.raises(BackendError) as failure:
                worker.execute(UUID(failed_fetch.json()["task_id"]))
        assert failure.value.code == "repository_failed"
        assert client.get(url).json()["index_summary"] == first.json()["index_summary"]
        assert client.post(url + "/ask", json={"question": "version"}).status_code == 200
        assert list(state.agent_runs.values())[-1].snapshot_id == first_snapshot.id

        second_commit = bare_remote.commit_and_push(
            "second.py", "value = 2\n", "second",
        )
        with monkeypatch.context() as patch:
            patch.setattr(
                adapter.engine, "graph",
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    PermissionError("private graph detail")
                ),
            )
            failed_build = worker.submit(client, url)
            with pytest.raises(PermissionError):
                worker.execute(UUID(failed_build.json()["task_id"]))
        snapshots = list(state.snapshots.values())
        second_snapshot = next(s for s in snapshots if s.commit_sha == second_commit)
        assert len(snapshots) == 2 and second_snapshot.status == "failed"
        assert state.snapshots[first_snapshot.id].status == "ready"
        assert state.projects[project_id].current_snapshot_id == first_snapshot.id
        failed_project = client.get(url).json()
        assert failed_project["status"] == "failed"
        assert failed_project["index_summary"] == first.json()["index_summary"]

        # ask explicitly falls back to current A and binds its AgentRun to A.
        answer = client.post(url + "/ask", json={"question": "old version"})
        assert answer.status_code == 200, answer.text
        assert list(state.agent_runs.values())[-1].snapshot_id == first_snapshot.id

        repaired = worker.run(client, url)
        assert repaired.status_code == 200, repaired.text
        assert repaired.json()["index_summary"]["file_count"] == 2
        assert state.projects[project_id].current_snapshot_id == second_snapshot.id
        assert state.snapshots[second_snapshot.id].status == "ready"
        assert len(state.snapshots) == 2
        assert first_index.read_bytes() == first_bytes
        assert len(load_index(first_index).files) == 1

        # Same Commit + valid bundle performs Git refresh/checkout but no Core build.
        with monkeypatch.context() as patch:
            forbidden = lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("same Commit must reuse its ready Snapshot")
            )
            patch.setattr(adapter.engine, "index", forbidden)
            patch.setattr(adapter.engine, "graph", forbidden)
            repeated = worker.run(client, url)
        assert repeated.status_code == 200, repeated.text
        assert len(state.snapshots) == 2
        repository = app.state.workspace.path_for(project_id)
        assert git(repository, "rev-parse", "HEAD").decode("ascii").strip() == second_commit
        symbolic = subprocess.run(
            ["git", "symbolic-ref", "-q", "HEAD"], cwd=repository,
            capture_output=True, check=False, timeout=15,
        )
        assert symbolic.returncode == 1 and not symbolic.stdout

    # A new FastAPI instance can use persisted Project/Snapshot and files.
    restarted = create_app(
        adapter=adapter,
        workspace=RepositoryWorkspace(workspace_root),
        persistence=state,
    )
    with TestClient(restarted) as client:
        restored = client.get(url)
        assert restored.status_code == 200
        assert restored.json()["index_summary"]["file_count"] == 2
        answer = client.post(url + "/impact", json={"file_path": "second.py"})
        assert answer.status_code == 200, answer.text
        assert list(state.agent_runs.values())[-1].snapshot_id == second_snapshot.id


def test_existing_shallow_workspace_is_unshallowed_before_refresh(
    tmp_path, bare_remote, bare_transport,
):
    second_commit = bare_remote.commit_and_push(
        "second.py", "value = 2\n", "second",
    )
    repository = tmp_path / "shallow"
    subprocess.run(
        [
            "git", "-c", "protocol.file.allow=always", "clone", "--depth", "1",
            "--branch", "main", "--", bare_remote.bare.as_uri(), str(repository),
        ],
        capture_output=True, check=True, timeout=15,
    )
    assert git(repository, "rev-parse", "--is-shallow-repository").strip() == b"true"

    revision = GitRepositorySource().update(URL, None, repository)

    assert revision.branch == "main" and revision.commit_sha == second_commit
    assert git(repository, "rev-parse", "--is-shallow-repository").strip() == b"false"
    assert git(repository, "rev-parse", f"{bare_remote.first_commit}^{{commit}}").strip()


def test_head_change_after_build_prevents_artifact_publish_and_ready(
    tmp_path, bare_remote, bare_transport, adapter, monkeypatch,
    index_worker_factory, task_dispatcher_factory,
):
    target_commit = bare_remote.commit_and_push(
        "second.py", "value = 2\n", "second",
    )
    state = InMemoryPersistence()
    app = create_app(
        adapter=adapter,
        workspace=RepositoryWorkspace(tmp_path / "managed"),
        persistence=state,
        task_dispatcher=task_dispatcher_factory(),
    )
    worker = index_worker_factory(app)
    original = adapter.index

    def move_head_after_build(root, **kwargs):
        summary = original(root, **kwargs)
        git(root, "checkout", "--detach", "HEAD^")
        return summary

    monkeypatch.setattr(adapter, "index", move_head_after_build)
    with TestClient(app) as client:
        url, project_id = register(client)
        response = worker.submit(client, url)
        with pytest.raises(BackendError) as failure:
            worker.execute(UUID(response.json()["task_id"]))
        assert failure.value.code == "repository_changed"

    snapshot = next(iter(state.snapshots.values()))
    assert snapshot.commit_sha == target_commit and snapshot.status == "failed"
    assert state.projects[project_id].current_snapshot_id is None
    artifact_root = tmp_path / "managed" / str(project_id) / "artifacts"
    assert not [path for path in artifact_root.iterdir() if path.is_dir()]


def test_first_attempt_a_b_c_refreshes_keep_git_snapshot_and_artifacts_aligned(
    tmp_path, bare_remote, bare_transport, adapter,
    index_worker_factory, task_dispatcher_factory,
):
    state = InMemoryPersistence()
    app = create_app(
        adapter=adapter,
        workspace=RepositoryWorkspace(tmp_path / "managed"),
        persistence=state,
        task_dispatcher=task_dispatcher_factory(),
    )
    worker = index_worker_factory(app)
    with TestClient(app) as client:
        url, project_id = register(client)
        expected_commits = [bare_remote.first_commit]
        repository = app.state.workspace.path_for(project_id)

        for position in range(3):
            if position:
                expected_commits.append(bare_remote.commit_and_push(
                    f"version_{position}.py", f"VERSION = {position}\n",
                    f"version {position}",
                ))
            expected = expected_commits[-1]
            response = worker.run(client, url)
            assert response.status_code == 200, response.text

            snapshots = list(state.snapshots.values())
            current_id = state.projects[project_id].current_snapshot_id
            current = state.snapshots[current_id]
            assert len(snapshots) == position + 1
            assert current.commit_sha == expected and current.status == "ready"
            assert git(repository, "rev-parse", "HEAD").decode().strip() == expected
            assert git(
                repository, "rev-parse", "refs/remotes/origin/main^{commit}",
            ).decode().strip() == expected
            assert git(
                repository, "rev-parse",
                f"refs/pycode/snapshots/{current.id}^{{commit}}",
            ).decode().strip() == expected
            for snapshot in snapshots:
                bundle = repository.parent / "artifacts" / str(snapshot.id)
                assert (bundle / "index.json").is_file()
                assert (bundle / "code_graph.json").is_file()
                assert (bundle / "manifest.json").is_file()

        assert {snapshot.commit_sha for snapshot in state.snapshots.values()} == set(
            expected_commits
        )
