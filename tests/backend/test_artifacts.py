from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from backend.app.core.errors import BackendError
from backend.app.core.models import ProjectSnapshot
from backend.app.infrastructure.artifacts import SnapshotArtifactStore
from backend.app.integrations.pycode import PyCodeAdapter


def _snapshot(project_id, snapshot_id, commit_sha, bundle):
    now = datetime.now(timezone.utc)
    return ProjectSnapshot(
        snapshot_id, project_id, commit_sha,
        bundle.index_artifact_path, bundle.graph_artifact_path,
        bundle.summary.file_count, bundle.summary.node_count,
        bundle.summary.edge_count, "ready", now, now,
    )


def _workspace(tmp_path: Path):
    root = tmp_path / "managed"
    project_id, snapshot_id = uuid4(), uuid4()
    repo = root / str(project_id) / "repo"
    repo.mkdir(parents=True)
    (repo / "main.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    return root, project_id, snapshot_id, repo


def _publish(store, adapter, project_id, snapshot_id, commit_sha, repo):
    return store.build_and_publish(
        project_id, snapshot_id, commit_sha,
        lambda index_path, graph_path: adapter.index(
            repo, index_path=index_path, graph_path=graph_path,
        ),
    )


def test_snapshot_bundles_are_independent_and_validated(tmp_path):
    root, project_id, first_id, repo = _workspace(tmp_path)
    second_id = uuid4()
    store, adapter = SnapshotArtifactStore(root), PyCodeAdapter()
    first_bundle = _publish(store, adapter, project_id, first_id, "a" * 40, repo)
    first_bytes = first_bundle.index_path.read_bytes()

    (repo / "second.py").write_text("value = 2\n", encoding="utf-8")
    second_bundle = _publish(store, adapter, project_id, second_id, "b" * 40, repo)

    first = _snapshot(project_id, first_id, "a" * 40, first_bundle)
    second = _snapshot(project_id, second_id, "b" * 40, second_bundle)
    assert store.validate(project_id, first).summary.file_count == 1
    assert store.validate(project_id, second).summary.file_count == 2
    assert first_bundle.index_path.read_bytes() == first_bytes
    assert first_bundle.index_path.parent != second_bundle.index_path.parent


def test_windows_safe_repair_restores_old_directory_when_publish_rename_fails(
    tmp_path, monkeypatch,
):
    root, project_id, snapshot_id, repo = _workspace(tmp_path)
    store, adapter = SnapshotArtifactStore(root), PyCodeAdapter()
    commit_sha = "c" * 40
    original_bundle = _publish(store, adapter, project_id, snapshot_id, commit_sha, repo)
    original_snapshot = _snapshot(
        project_id, snapshot_id, commit_sha, original_bundle,
    )
    original_bytes = original_bundle.index_path.read_bytes()
    (repo / "new.py").write_text("new = True\n", encoding="utf-8")

    original_rename = Path.rename
    final = root / str(project_id) / "artifacts" / str(snapshot_id)
    failed = False

    def fail_new_bundle(source, target):
        nonlocal failed
        if source.name == "bundle" and Path(target) == final and not failed:
            failed = True
            raise PermissionError("simulated Windows directory replacement failure")
        return original_rename(source, target)

    monkeypatch.setattr(Path, "rename", fail_new_bundle)
    with pytest.raises(BackendError) as failure:
        _publish(store, adapter, project_id, snapshot_id, commit_sha, repo)
    assert failure.value.code == "artifact_publish_failed"
    assert failed and final.is_dir()
    assert store.validate(project_id, original_snapshot).index_path.read_bytes() == original_bytes
    assert not list(final.parent.glob(f".{snapshot_id}.backup-*"))


def test_snapshot_repair_replaces_bundle_without_os_replace(tmp_path):
    root, project_id, snapshot_id, repo = _workspace(tmp_path)
    store, adapter = SnapshotArtifactStore(root), PyCodeAdapter()
    commit_sha = "d" * 40
    first_bundle = _publish(store, adapter, project_id, snapshot_id, commit_sha, repo)
    first = _snapshot(project_id, snapshot_id, commit_sha, first_bundle)
    assert store.validate(project_id, first).summary.file_count == 1

    (repo / "repaired.py").write_text("repaired = True\n", encoding="utf-8")
    repaired_bundle = _publish(store, adapter, project_id, snapshot_id, commit_sha, repo)
    repaired = replace(
        first,
        file_count=repaired_bundle.summary.file_count,
        node_count=repaired_bundle.summary.node_count,
        edge_count=repaired_bundle.summary.edge_count,
    )
    assert store.validate(project_id, repaired).summary.file_count == 2
    assert not list(repaired_bundle.index_path.parent.parent.glob(
        f".{snapshot_id}.backup-*"
    ))
