import subprocess
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from backend.app.config import RepositoryRetrySettings
from backend.app.core.errors import BackendError
from backend.app.core.models import IndexSummary
from backend.app.infrastructure.repositories import GitRepositorySource
from backend.app.infrastructure.retry import run_with_bounded_retry
from backend.app.repositories import InMemoryPersistence, InMemoryUnitOfWork
from backend.app.services.background_tasks import BackgroundTaskService


URL = "https://github.com/example/public.git"
SHA = "a" * 40
FAST_RETRY = RepositoryRetrySettings(2, 1, 5)


def _source(monkeypatch, run, delays):
    monkeypatch.setattr(subprocess, "run", run)
    return GitRepositorySource(
        retry_settings=FAST_RETRY,
        sleeper=delays.append,
        jitter=lambda: 0,
    )


def test_clone_timeout_retries_once_after_removing_partial_staging(
    tmp_path, monkeypatch,
):
    parent = tmp_path / "owned"
    parent.mkdir()
    destination = parent / "repo"
    clone_calls = 0
    delays = []

    def run(command, **kwargs):
        nonlocal clone_calls
        if "ls-remote" in command:
            return subprocess.CompletedProcess(command, 0, b"", b"")
        if "clone" in command:
            clone_calls += 1
            if clone_calls == 1:
                destination.mkdir()
                (destination / "partial").write_text("partial", encoding="utf-8")
                raise subprocess.TimeoutExpired(command, 120)
            assert not destination.exists()
            destination.mkdir()
            (destination / ".git").mkdir()
            return subprocess.CompletedProcess(command, 0, b"", b"")
        if "rev-parse" in command:
            return subprocess.CompletedProcess(command, 0, (SHA + "\n").encode(), b"")
        if "ls-tree" in command:
            return subprocess.CompletedProcess(command, 0, b"", b"")
        raise AssertionError(command)

    source = _source(monkeypatch, run, delays)
    revision = source.clone(URL, "main", destination, project_id=uuid4())
    assert revision.commit_sha == SHA
    assert clone_calls == 2
    assert delays == [1]


def test_fetch_timeout_replays_the_exact_same_refspec(tmp_path, monkeypatch):
    repository = tmp_path / "repo"
    repository.mkdir()
    fetch_commands = []
    delays = []

    def run(command, **kwargs):
        if "ls-remote" in command:
            return subprocess.CompletedProcess(command, 0, b"", b"")
        if "rev-parse" in command and "--is-shallow-repository" in command:
            return subprocess.CompletedProcess(command, 0, b"false\n", b"")
        if "fetch" in command:
            fetch_commands.append(list(command))
            if len(fetch_commands) == 1:
                raise subprocess.TimeoutExpired(command, 120)
            return subprocess.CompletedProcess(command, 0, b"", b"")
        if "rev-parse" in command:
            return subprocess.CompletedProcess(command, 0, (SHA + "\n").encode(), b"")
        if "ls-tree" in command:
            return subprocess.CompletedProcess(command, 0, b"", b"")
        raise AssertionError(command)

    revision = _source(monkeypatch, run, delays).update(URL, "main", repository)
    assert revision.commit_sha == SHA
    assert len(fetch_commands) == 2
    assert fetch_commands[0] == fetch_commands[1]
    assert "+refs/heads/main:refs/remotes/origin/main" in fetch_commands[0]
    assert delays == [1]


def test_network_timeout_stops_at_configured_max_attempts(tmp_path, monkeypatch):
    parent = tmp_path / "owned"
    parent.mkdir()
    calls = []
    delays = []

    def timeout(command, **kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, 120)

    source = _source(monkeypatch, timeout, delays)
    with pytest.raises(BackendError) as failure:
        source.clone(URL, "main", parent / "repo")
    assert failure.value.code == "repository_timeout"
    assert len(calls) == 2
    assert delays == [1]


@pytest.mark.parametrize("returncode,code", [(2, "repository_branch_missing"), (128, "repository_failed")])
def test_permanent_and_generic_git_failures_do_not_retry(
    tmp_path, monkeypatch, returncode, code,
):
    parent = tmp_path / "owned"
    parent.mkdir()
    calls = []
    delays = []

    def fail(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(returncode, command, stderr=b"safe")

    with pytest.raises(BackendError) as failure:
        _source(monkeypatch, fail, delays).clone(URL, "missing", parent / "repo")
    assert failure.value.code == code
    assert len(calls) == 1
    assert delays == []


def test_invalid_url_is_rejected_without_operation_attempt(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(BackendError) as failure:
        GitRepositorySource().clone("file:///unsafe", None, tmp_path / "repo")
    assert failure.value.code == "invalid_repository"
    assert calls == []


@pytest.mark.parametrize("code", ["repository_changed", "unsafe_repository", "repository_failed"])
def test_non_timeout_backend_errors_are_not_retried_in_network_stage(
    tmp_path, monkeypatch, code,
):
    source = GitRepositorySource(
        retry_settings=FAST_RETRY,
        sleeper=lambda delay: pytest.fail("must not sleep"),
    )
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise BackendError(code, "safe")

    monkeypatch.setattr(source, "_run_once", fail)
    with pytest.raises(BackendError) as failure:
        source._run(
            ["ls-remote"], cwd=tmp_path, control=tmp_path,
            stage="resolve_remote_branch",
        )
    assert failure.value.code == code
    assert calls == [1]


def test_backoff_is_bounded_and_tests_inject_sleep():
    attempts = []
    delays = []

    def fail():
        attempts.append(1)
        raise BackendError("repository_timeout", "safe")

    with pytest.raises(BackendError):
        run_with_bounded_retry(
            fail,
            operation_name="fetch_remote_branch",
            project_id=None,
            settings=RepositoryRetrySettings(4, 4, 5),
            should_retry=lambda exc: True,
            sleeper=delays.append,
            jitter=lambda: 1,
            logger=__import__("logging").getLogger("test"),
        )
    assert len(attempts) == 4
    assert delays == [5, 5, 5]


def test_stage_local_retry_does_not_change_business_or_enqueue_attempt_counts():
    persistence = InMemoryPersistence()
    uow = InMemoryUnitOfWork(persistence)
    with uow.transaction():
        project = uow.projects.create("demo", URL)
        snapshot = uow.snapshots.upsert(
            project.id,
            SHA,
            index_artifact_path="index.json",
            graph_artifact_path="graph.json",
            summary=IndexSummary(1, 1, 0),
        )
        task = uow.background_tasks.create("repository_index", project_id=project.id)
    calls = []
    delays = []

    def index_project(project_id):
        def operation():
            calls.append(1)
            if len(calls) == 1:
                raise BackendError("repository_timeout", "safe")
            return replace(project, current_snapshot_id=snapshot.id)

        return run_with_bounded_retry(
            operation,
            operation_name="fetch_remote_branch",
            project_id=project_id,
            settings=FAST_RETRY,
            should_retry=lambda exc: getattr(exc, "code", None) == "repository_timeout",
            sleeper=delays.append,
            jitter=lambda: 0,
            logger=__import__("logging").getLogger("test"),
        )

    completed = BackgroundTaskService(uow).execute_repository_index(
        task.id, index_project,
    )
    assert len(calls) == 2 and delays == [1]
    assert completed.attempt_count == 1
    assert completed.enqueue_attempt_count == 0
