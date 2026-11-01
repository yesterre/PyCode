from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
from uuid import UUID, uuid4

import pytest

from backend.app.config import StaleReconciliationSettings
from backend.app.core.errors import BackendError
from backend.app.core.models import ProjectStatus
from backend.app.db.models import BackgroundTaskORM
from backend.app.infrastructure.artifacts import SnapshotArtifactStore
from backend.app.integrations.pycode import PyCodeAdapter
from backend.app.infrastructure.repositories import RepositoryWorkspace
from backend.app.main import create_app
from backend.app.repositories import InMemoryPersistence
from backend.app.services.background_tasks import REPOSITORY_INDEX_TASK_TYPE
from backend.app.services.stale_reconciliation import StaleRunningReconciler
from test_artifacts import _publish, _snapshot, _workspace
from test_b01_users import uow_factory
from test_b02_auth import auth_context
from test_b04_project_derived_auth import resources
from test_b07_worker_isolation import workers, ready, mutate
from test_postgresql import postgresql_engine
from test_phase4_lifecycle import bare_remote, bare_transport, register


def publish_chunks(store, adapter, project_id, snapshot_id, commit, repo, **kwargs):
    return store.build_and_publish(
        project_id, snapshot_id, commit,
        lambda index_path, graph_path: adapter.index(
            repo, index_path=index_path, graph_path=graph_path,
            project_id=project_id, snapshot_id=snapshot_id, commit_sha=commit,
        ), require_chunks=True, **kwargs,
    )


def legacy_bundle(store, snapshot):
    """Simulate a real pre-A01 bundle without changing any base bytes."""
    bundle = store.read_ready(snapshot.project_id, snapshot)
    manifest_path = bundle.index_path.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = 1
    for key in ("chunks_schema_version", "chunks_sha256", "chunk_count"):
        manifest.pop(key, None)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    if bundle.chunks_path is not None:
        bundle.chunks_path.unlink()
    return store.read_ready(snapshot.project_id, snapshot)


def current(application, project_id):
    state = application.state.persistence
    project = state.projects[project_id]
    return state.snapshots[project.current_snapshot_id]


def test_v1_remains_readable_and_v2_chunks_are_identity_checked(tmp_path):
    root, project_id, first_id, repo = _workspace(tmp_path)
    store, adapter = SnapshotArtifactStore(root), PyCodeAdapter()
    first = _publish(store, adapter, project_id, first_id, "a" * 40, repo)
    snapshot = _snapshot(project_id, first_id, "a" * 40, first)
    assert store.read_chunks(project_id, snapshot) is None
    base = (first.index_path.read_bytes(), first.graph_path.read_bytes())
    upgraded = publish_chunks(store, adapter, project_id, first_id, "a" * 40, repo, preserve_base=True)
    assert (upgraded.index_path.read_bytes(), upgraded.graph_path.read_bytes()) == base
    manifest = json.loads((upgraded.index_path.parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == 2 and manifest["chunks_schema_version"] == 1
    chunks = store.read_chunks(project_id, snapshot)
    assert len(chunks.chunks) == manifest["chunk_count"]
    assert chunks.binding.snapshot_id == str(snapshot.id)
    for forged in (replace(snapshot, project_id=uuid4()), replace(snapshot, id=uuid4()),
                   replace(snapshot, commit_sha="b" * 40)):
        with pytest.raises(BackendError):
            store.read_chunks(project_id, forged)


def test_failed_post_publish_validation_restores_original_v1(tmp_path, monkeypatch):
    root, project_id, snapshot_id, repo = _workspace(tmp_path)
    store, adapter = SnapshotArtifactStore(root), PyCodeAdapter()
    original = _publish(store, adapter, project_id, snapshot_id, "a" * 40, repo)
    snapshot = _snapshot(project_id, snapshot_id, "a" * 40, original)
    before = {path.name: path.read_bytes() for path in original.index_path.parent.iterdir()}
    read = store._read_bundle
    failed = []

    def reject_replacement(*args, **kwargs):
        if not failed and (original.index_path.parent / "chunks.json").exists() and list(
                original.index_path.parent.parent.glob(f".{snapshot_id}.backup-*")):
            failed.append(True)
            raise BackendError("artifacts_unavailable", "simulated final validation failure")
        return read(*args, **kwargs)

    monkeypatch.setattr(store, "_read_bundle", reject_replacement)
    with pytest.raises(BackendError):
        publish_chunks(store, adapter, project_id, snapshot_id, "a" * 40, repo, preserve_base=True)
    assert failed
    assert {path.name: path.read_bytes() for path in original.index_path.parent.iterdir()} == before
    assert store.validate(project_id, snapshot).chunks_path is None


def test_chunk_upgrade_preserves_older_graph_counts(tmp_path):
    root, project_id, snapshot_id, repo = _workspace(tmp_path)
    store, adapter = SnapshotArtifactStore(root), PyCodeAdapter()
    bundle = _publish(store, adapter, project_id, snapshot_id, "a" * 40, repo)
    graph = json.loads(bundle.graph_path.read_text(encoding="utf-8"))
    graph.update(nodes=[], edges=[])
    bundle.graph_path.write_text(json.dumps(graph), encoding="utf-8")
    manifest_path = bundle.index_path.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(node_count=0, edge_count=0, graph_sha256=store._sha256(bundle.graph_path))
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    old_graph = bundle.graph_path.read_bytes()
    upgraded = publish_chunks(store, adapter, project_id, snapshot_id, "a" * 40, repo, preserve_base=True)
    assert upgraded.graph_path.read_bytes() == old_graph
    assert upgraded.summary.node_count == 0 and upgraded.chunks_path is not None


def test_recovery_restores_valid_backup_when_new_directory_is_corrupt(tmp_path):
    root, project_id, snapshot_id, repo = _workspace(tmp_path)
    store, adapter = SnapshotArtifactStore(root), PyCodeAdapter()
    original = _publish(store, adapter, project_id, snapshot_id, "a" * 40, repo)
    directory = original.index_path.parent
    backup = directory.parent / f".{snapshot_id}.backup-{uuid4()}"
    shutil.copytree(directory, backup)
    (directory / "manifest.json").write_text("broken", encoding="utf-8")
    restored = store.recover_published(project_id, snapshot_id, "a" * 40)
    assert restored.summary == original.summary and restored.chunks_path is None
    assert not backup.exists()


@pytest.mark.parametrize("damage", ["missing", "content", "identity", "schema"])
def test_v2_reader_rejects_invalid_chunk_artifact(tmp_path, damage):
    root, project_id, snapshot_id, repo = _workspace(tmp_path)
    store = SnapshotArtifactStore(root)
    bundle = publish_chunks(store, PyCodeAdapter(), project_id, snapshot_id, "a" * 40, repo)
    snapshot = _snapshot(project_id, snapshot_id, "a" * 40, bundle)
    if damage == "missing":
        bundle.chunks_path.unlink()
    else:
        data = json.loads(bundle.chunks_path.read_text(encoding="utf-8"))
        if damage == "content":
            data["chunks"][0]["content"] = "not the source\n"
        elif damage == "identity":
            data["binding"]["project_id"] = str(uuid4())
        else:
            data["schema_version"] = 99
        bundle.chunks_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(BackendError) as failure:
        store.read_chunks(project_id, snapshot)
    assert failure.value.code == "artifacts_unavailable"


def test_chunk_path_escape_is_rejected_before_read(tmp_path, monkeypatch):
    root, project_id, snapshot_id, repo = _workspace(tmp_path)
    store = SnapshotArtifactStore(root)
    bundle = publish_chunks(store, PyCodeAdapter(), project_id, snapshot_id, "a" * 40, repo)
    snapshot = _snapshot(project_id, snapshot_id, "a" * 40, bundle)
    original = Path.resolve
    outside = tmp_path / "outside.json"
    monkeypatch.setattr(Path, "resolve", lambda path, *a, **k: outside if path == bundle.chunks_path else original(path, *a, **k))
    with pytest.raises(BackendError) as failure:
        store.read_chunks(project_id, snapshot)
    assert failure.value.code == "unsafe_repository"


def test_real_index_worker_parses_once_reuses_chunks_and_ignores_redelivery(
    client, project_url, repository, application, index_worker, monkeypatch,
):
    import pycode.parser
    calls = Counter()
    original = pycode.parser.ast.parse

    def parse(source, filename="<unknown>", *args, **kwargs):
        if str(filename).startswith(str(repository)):
            calls[str(filename)] += 1
        return original(source, filename, *args, **kwargs)

    monkeypatch.setattr(pycode.parser.ast, "parse", parse)
    accepted = index_worker.submit(client, project_url)
    task_id = UUID(accepted.json()["task_id"])
    index_worker.execute(task_id)
    assert calls == {str(repository / "main.py"): 1, str(repository / "service.py"): 1}
    project_id = UUID(project_url.rsplit("/", 1)[-1])
    snapshot = current(application, project_id)
    chunks = application.state.artifacts.read_chunks(project_id, snapshot)
    index_worker.execute(task_id)
    index_worker.run(client, project_url)
    assert calls == {str(repository / "main.py"): 1, str(repository / "service.py"): 1}
    assert application.state.artifacts.read_chunks(project_id, snapshot) == chunks


@pytest.mark.parametrize("fail_publish", [False, True])
def test_ready_v1_is_backfilled_and_failed_upgrade_preserves_base(
    client, ready_url, application, index_worker, monkeypatch, fail_publish,
):
    project_id = UUID(ready_url.rsplit("/", 1)[-1])
    snapshot = current(application, project_id)
    store = application.state.artifacts
    old = legacy_bundle(store, snapshot)
    before = {path.name: path.read_bytes() for path in old.index_path.parent.iterdir()}
    assert client.post(ready_url + "/ask", json={"question": "entry"}).status_code == 200
    def forbidden(*args, **kwargs):
        raise AssertionError("Backfill must not rebuild valid base Index/Graph")

    monkeypatch.setattr(application.state.adapter.engine, "index", forbidden)
    monkeypatch.setattr(application.state.adapter.engine, "graph", forbidden)
    original = Path.rename
    failures = []

    def rename(path, target):
        if fail_publish and path.name == "bundle" and Path(target) == old.index_path.parent and not failures:
            failures.append(True)
            raise PermissionError("simulated upgrade publish failure")
        return original(path, target)

    monkeypatch.setattr(Path, "rename", rename)
    accepted = index_worker.submit(client, ready_url)
    task_id = UUID(accepted.json()["task_id"])
    if fail_publish:
        with pytest.raises(BackendError):
            index_worker.execute(task_id)
        assert failures
        assert {path.name: path.read_bytes() for path in old.index_path.parent.iterdir()} == before
        assert application.state.persistence.background_tasks[task_id].status == "failed"
    else:
        index_worker.execute(task_id)
        assert store.read_chunks(project_id, snapshot) is not None
        assert old.index_path.read_bytes() == before["index.json"]
        assert old.graph_path.read_bytes() == before["code_graph.json"]
    assert current(application, project_id).id == snapshot.id
    assert application.state.persistence.snapshots[snapshot.id].status == "ready"
    assert client.get(ready_url).json()["status"] == ("failed" if fail_publish else "ready")
    assert client.post(ready_url + "/ask", json={"question": "entry"}).status_code == 200


@pytest.mark.parametrize("exhausted", [False, True])
def test_stale_backfill_requeues_and_worker_recovers_exact_snapshot(
    resources, workers, monkeypatch, exhausted,
):
    r = resources
    ready(r, workers)
    state = r.context.app.state
    with r.context.uow_factory() as uow, uow.transaction():
        project = uow.projects.get(r.own)
        snapshot = uow.snapshots.get(project.current_snapshot_id)
        task = uow.background_tasks.create(REPOSITORY_INDEX_TASK_TYPE, project_id=r.own, snapshot_id=snapshot.id)
        task = uow.background_tasks.claim(task.id).task
        mutate(uow, "background_tasks", BackgroundTaskORM, task.id,
               lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        uow.projects.set_status(r.own, ProjectStatus.INDEXING)
    legacy_bundle(state.artifacts, snapshot)
    monkeypatch.setattr(state.workspace, "synchronize", lambda *a: (_ for _ in ()).throw(AssertionError("recovery must not resolve HEAD")))
    with r.context.uow_factory() as uow:
        outcome = StaleRunningReconciler(uow, state.project_operations,
                     StaleReconciliationSettings(max_execution_attempts=1 if exhausted else 2)).reconcile()
    if exhausted:
        assert outcome.failed == 1 and outcome.completed == 0
        assert state.artifacts.read_chunks(r.own, snapshot) is None
    else:
        assert outcome.requeued == 1 and outcome.completed == 0
        workers.index(str(task.id))
        chunks = state.artifacts.read_chunks(r.own, snapshot)
        assert chunks.binding.snapshot_id == str(snapshot.id)
        workers.index(str(task.id))
    with r.context.uow_factory() as uow, uow.transaction():
        assert uow.snapshots.get(snapshot.id).status == "ready"
        assert uow.projects.get(r.own).status == "ready"
        assert uow.background_tasks.get(task.id).status == ("failed" if exhausted else "completed")


def test_two_users_worker_chunks_remain_project_isolated(resources, workers):
    r = resources
    for project_id, headers in ((r.own, r.headers), (r.foreign, r.other_headers)):
        response = r.context.client.post(f"/api/v1/projects/{project_id}/index", headers=headers)
        assert response.status_code == 202
        workers.index(response.json()["task_id"])
    with r.context.uow_factory() as uow, uow.transaction():
        own = uow.snapshots.get(uow.projects.get(r.own).current_snapshot_id)
        foreign = uow.snapshots.get(uow.projects.get(r.foreign).current_snapshot_id)
    store = r.context.app.state.artifacts
    left, right = store.read_chunks(r.own, own), store.read_chunks(r.foreign, foreign)
    assert {chunk.id for chunk in left.chunks}.isdisjoint(chunk.id for chunk in right.chunks)
    with pytest.raises(BackendError):
        store.read_chunks(r.own, foreign)
    denied = r.context.client.get(f"/api/v1/projects/{r.foreign}/snapshots/{foreign.id}", headers=r.headers)
    assert denied.status_code == 404


def test_worker_crash_after_chunk_publish_recovers_without_reparsing(resources, workers, monkeypatch):
    r = resources
    ready(r, workers)
    state = r.context.app.state
    with r.context.uow_factory() as uow, uow.transaction():
        project = uow.projects.get(r.own)
        snapshot = uow.snapshots.get(project.current_snapshot_id)
    old = legacy_bundle(state.artifacts, snapshot)
    base_bytes = old.index_path.read_bytes()
    accepted = r.context.client.post(f"/api/v1/projects/{r.own}/index", headers=r.headers)
    task_id = UUID(accepted.json()["task_id"])
    publish = state.artifacts._publish

    class ProcessCrash(BaseException):
        pass

    def publish_then_crash(*args, **kwargs):
        publish(*args, **kwargs)
        raise ProcessCrash()

    with monkeypatch.context() as patch:
        patch.setattr(state.artifacts, "_publish", publish_then_crash)
        with pytest.raises(ProcessCrash):
            workers.index(str(task_id))
    chunks = state.artifacts.read_chunks(r.own, snapshot)
    assert old.index_path.read_bytes() == base_bytes
    with r.context.uow_factory() as uow, uow.transaction():
        assert uow.background_tasks.get(task_id).status == "running"
        mutate(uow, "background_tasks", BackgroundTaskORM, task_id,
               lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    with r.context.uow_factory() as uow:
        outcome = StaleRunningReconciler(uow, state.project_operations).reconcile()
    assert outcome.requeued == 1 and outcome.completed == 0
    monkeypatch.setattr(state.adapter, "index", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must reuse published chunks")))
    monkeypatch.setattr(state.adapter, "build_chunks", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must reuse published chunks")))
    monkeypatch.setattr(state.workspace, "synchronize", lambda *a: (_ for _ in ()).throw(AssertionError("must not resolve HEAD")))
    workers.index(str(task_id))
    assert state.artifacts.read_chunks(r.own, snapshot) == chunks
    with r.context.uow_factory() as uow, uow.transaction():
        assert uow.background_tasks.get(task_id).status == "completed"
        assert uow.projects.get(r.own).status == "ready"


def test_failed_backfill_is_not_misreported_complete_if_task_terminal_write_crashes(resources, workers, monkeypatch):
    r = resources
    ready(r, workers)
    state = r.context.app.state
    with r.context.uow_factory() as uow, uow.transaction():
        snapshot = uow.snapshots.get(uow.projects.get(r.own).current_snapshot_id)
        repository_type = type(uow.background_tasks)
    old = legacy_bundle(state.artifacts, snapshot)
    before = old.index_path.read_bytes()
    accepted = r.context.client.post(f"/api/v1/projects/{r.own}/index", headers=r.headers)
    task_id = UUID(accepted.json()["task_id"])

    class ProcessCrash(BaseException):
        pass

    with monkeypatch.context() as patch:
        patch.setattr(state.adapter, "build_chunks", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulated chunk failure")))
        patch.setattr(repository_type, "mark_failed", lambda *a, **k: (_ for _ in ()).throw(ProcessCrash()))
        with pytest.raises(ProcessCrash):
            workers.index(str(task_id))
    with r.context.uow_factory() as uow, uow.transaction():
        assert uow.background_tasks.get(task_id).status == "running"
        assert uow.projects.get(r.own).status == "failed"
        mutate(uow, "background_tasks", BackgroundTaskORM, task_id,
               lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    with r.context.uow_factory() as uow:
        outcome = StaleRunningReconciler(uow, state.project_operations).reconcile()
    assert outcome.failed == 1 and outcome.completed == 0
    assert old.index_path.read_bytes() == before
    assert state.artifacts.read_chunks(r.own, snapshot) is None
    assert r.context.client.post(f"/api/v1/projects/{r.own}/ask", headers=r.headers,
                                 json={"question": "entry"}).status_code == 200
    with r.context.uow_factory() as uow, uow.transaction():
        assert uow.snapshots.get(snapshot.id).status == "ready"
        assert uow.projects.get(r.own).current_snapshot_id == snapshot.id


def test_snapshot_switch_and_failed_new_publish_keep_old_chunks(
    tmp_path, bare_remote, bare_transport, adapter, authenticated_client_factory,
    index_worker_factory, task_dispatcher_factory, monkeypatch,
):
    app = create_app(adapter=adapter, workspace=RepositoryWorkspace(tmp_path / "managed"),
                     persistence=InMemoryPersistence(), task_dispatcher=task_dispatcher_factory())
    worker = index_worker_factory(app)
    with authenticated_client_factory(app) as client:
        url, project_id = register(client)
        worker.run(client, url)
        old = current(app, project_id)
        old_chunks = app.state.artifacts.read_chunks(project_id, old)
        old_path = app.state.artifacts.read_ready(project_id, old).chunks_path
        before = old_path.read_bytes()
        second_commit = bare_remote.commit_and_push("new.py", "def new():\n    return 2\n", "new")
        with monkeypatch.context() as patch:
            patch.setattr(app.state.artifacts, "_publish", lambda *a: (_ for _ in ()).throw(
                BackendError("artifact_publish_failed", "simulated new Snapshot failure")))
            accepted = worker.submit(client, url)
            with pytest.raises(BackendError):
                worker.execute(UUID(accepted.json()["task_id"]))
        assert current(app, project_id).id == old.id
        assert old_path.read_bytes() == before
        assert app.state.artifacts.read_chunks(project_id, old) == old_chunks
        assert client.post(url + "/ask", json={"question": "entry"}).status_code == 200
        worker.run(client, url)
        new = current(app, project_id)
        new_chunks = app.state.artifacts.read_chunks(project_id, new)
        assert new.commit_sha == second_commit and new.id != old.id
        assert {chunk.id for chunk in old_chunks.chunks}.isdisjoint(chunk.id for chunk in new_chunks.chunks)
        assert old_path.read_bytes() == before
        assert {file.path for file in new_chunks.files} == {"main.py", "new.py"}
