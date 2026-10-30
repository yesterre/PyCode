import os
from dataclasses import replace
from uuid import uuid4

import pytest

from backend.app.core.errors import BackendError
from backend.app.infrastructure.agent_workspace import AgentSnapshotWorkspace
from backend.app.infrastructure.artifacts import SnapshotArtifactStore
from backend.app.integrations.pycode import PyCodeAdapter
from test_artifacts import _publish, _snapshot, _workspace


def directory_link(target, link):
    # Junctions exercise Windows reparse protection without symlink privileges.
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)
    assert link.resolve() == target.resolve()


def remove_directory_link(link):
    if os.name == "nt":
        link.rmdir()
    else:
        link.unlink()


@pytest.fixture
def published(tmp_path):
    root, project_id, snapshot_id, repo = _workspace(tmp_path)
    store = SnapshotArtifactStore(root)
    bundle = _publish(store, PyCodeAdapter(), project_id, snapshot_id, "a" * 40, repo)
    return store, repo, _snapshot(project_id, snapshot_id, "a" * 40, bundle), bundle


@pytest.mark.parametrize("operation", ["validate", "read_ready", "archive_legacy"])
def test_artifact_reads_reject_snapshot_owned_by_another_project(published, operation):
    store, repo, snapshot, bundle = published
    forged = replace(snapshot, project_id=uuid4())
    before = bundle.index_path.read_bytes()
    with pytest.raises(BackendError) as failure:
        if operation == "archive_legacy":
            # Non-legacy fallback must enforce the same association.
            store.archive_legacy(snapshot.project_id, forged, repo)
        else:
            getattr(store, operation)(snapshot.project_id, forged)
    assert failure.value.code == "artifacts_unavailable"
    assert bundle.index_path.read_bytes() == before


@pytest.mark.parametrize("wrong_snapshot", [False, True])
def test_legacy_archive_cannot_import_another_projects_repository(published, wrong_snapshot):
    store, repo, snapshot, bundle = published
    AgentSnapshotWorkspace().prepare(repo, bundle)
    other_id = uuid4()
    (store.root / str(other_id) / "repo").mkdir(parents=True)
    legacy = replace(snapshot, index_artifact_path=".pclens/index.json",
                     graph_artifact_path=".pclens/code_graph.json")
    if wrong_snapshot:
        # Existing local legacy files cannot override persisted ownership.
        legacy = replace(legacy, project_id=other_id)
        project_id = snapshot.project_id
    else:
        project_id = other_id
        legacy = replace(legacy, project_id=other_id)
    with pytest.raises(BackendError) as failure:
        store.archive_legacy(project_id, legacy, repo)
    assert failure.value.code == "artifacts_unavailable"
    assert not (store.root / str(other_id) / "artifacts").exists()


def test_project_directory_alias_cannot_publish_into_another_project(published):
    store, repo, snapshot, bundle = published
    alias_id, new_snapshot = uuid4(), uuid4()
    alias = store.root / str(alias_id)
    before = bundle.index_path.read_bytes()
    directory_link(repo.parent, alias)
    called = []

    def build(index_path, graph_path):
        called.append(True)
        return PyCodeAdapter().index(repo, index_path=index_path, graph_path=graph_path)

    try:
        with pytest.raises(BackendError) as failure:
            store.build_and_publish(alias_id, new_snapshot, snapshot.commit_sha, build)
        assert failure.value.code == "unsafe_repository"
        assert called == []
        assert not (repo.parent / "artifacts" / str(new_snapshot)).exists()
        assert bundle.index_path.read_bytes() == before
    finally:
        remove_directory_link(alias)


def test_artifact_directory_alias_cannot_recover_backups_in_another_project(published):
    store, repo, snapshot, bundle = published
    project_id, snapshot_id = uuid4(), uuid4()
    project_root = store.root / str(project_id)
    project_root.mkdir()
    foreign_artifacts = bundle.index_path.parent.parent
    backup = foreign_artifacts / f".{snapshot_id}.backup-{uuid4()}"
    backup.mkdir()
    private = backup / "private.txt"
    private.write_text("foreign backup", encoding="utf-8")
    alias = project_root / "artifacts"
    directory_link(foreign_artifacts, alias)
    index_path, graph_path = store.relative_paths(snapshot_id)
    own = replace(snapshot, id=snapshot_id, project_id=project_id,
                  index_artifact_path=index_path, graph_artifact_path=graph_path)
    try:
        with pytest.raises(BackendError) as failure:
            store.validate(project_id, own)
        assert failure.value.code == "unsafe_repository"
        assert private.read_text(encoding="utf-8") == "foreign backup"
        assert not (foreign_artifacts / str(snapshot_id)).exists()
    finally:
        remove_directory_link(alias)


@pytest.mark.parametrize("source", ["both", "index", "graph"])
def test_agent_preparation_rejects_foreign_bundle_before_mutating_workspace(published, source):
    store, repo, snapshot, bundle = published
    other_id, other_snapshot = uuid4(), uuid4()
    other_repo = store.root / str(other_id) / "repo"
    other_repo.mkdir(parents=True)
    (other_repo / "secret.py").write_text("secret = 42\n", encoding="utf-8")
    foreign = _publish(store, PyCodeAdapter(), other_id, other_snapshot, "b" * 40, other_repo)
    forged = foreign if source == "both" else replace(
        bundle, **{source + "_path": getattr(foreign, source + "_path")},
    )
    before = foreign.index_path.read_bytes()
    with pytest.raises(BackendError) as failure:
        AgentSnapshotWorkspace().prepare(repo, forged)
    assert failure.value.code == "artifacts_unavailable"
    assert not (repo / ".pclens").exists()
    assert foreign.index_path.read_bytes() == before


def test_artifact_hardlink_remains_forbidden(published, tmp_path):
    store, repo, snapshot, bundle = published
    private = tmp_path / "private-index.json"
    private.write_bytes(bundle.index_path.read_bytes())
    bundle.index_path.unlink()
    os.link(private, bundle.index_path)
    with pytest.raises(BackendError) as failure:
        store.validate(snapshot.project_id, snapshot)
    assert failure.value.code == "unsafe_repository"


def test_agent_target_junction_remains_forbidden(published, tmp_path):
    store, repo, snapshot, bundle = published
    private = tmp_path / "private"
    private.mkdir()
    target = repo / ".pclens"
    directory_link(private, target)
    try:
        with pytest.raises(BackendError) as failure:
            AgentSnapshotWorkspace().prepare(repo, bundle)
        assert failure.value.code == "unsafe_repository"
        assert list(private.iterdir()) == []
    finally:
        remove_directory_link(target)
