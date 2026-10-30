from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import delete, update

from backend.app.core.errors import BackendError
from backend.app.core.models import IndexSummary, UserStatus
from backend.app.db.models import AgentRunORM, BackgroundTaskORM, ProjectORM, UserORM
from backend.app.repositories import InMemoryUnitOfWork
from backend.app.runtime import build_agent_run_service
from backend.app.services.background_tasks import BackgroundTaskService
from backend.app.tasks import agent_runs, indexing
from pycode.storage import load_index
from test_b01_users import uow_factory
from test_b02_auth import auth_context
from test_b04_project_derived_auth import project_service, resources
from test_b05_async_auth import async_resources, submit
from test_postgresql import postgresql_engine


def mutate(uow, repository, model, record_id, **values):
    """Simulate persisted corruption; SQL foreign keys remain enabled."""
    if isinstance(uow, InMemoryUnitOfWork):
        records = getattr(uow.state, repository)
        records[record_id] = replace(records[record_id], **values)
    else:
        uow.session.execute(update(model).where(model.id == record_id).values(**values))


@pytest.fixture
def workers(resources, application, monkeypatch):
    context = resources.context
    # Keep the existing lock implementation, with independent locks per Project.
    context.app.state.project_operations = application.state.project_operations
    published = []

    class Publisher:
        def publish(self, run_id, event):
            published.append((run_id, event))

    @contextmanager
    def index_services():
        with context.uow_factory() as uow:
            yield BackgroundTaskService(uow), project_service(context, uow)

    @contextmanager
    def agent_service():
        with context.uow_factory() as uow:
            yield build_agent_run_service(
                uow, background_tasks=BackgroundTaskService(uow),
                projects=project_service(context, uow), adapter=context.app.state.adapter,
                agent_workspace=context.app.state.agent_workspace, trace_publisher=Publisher(),
            )

    monkeypatch.setattr(indexing, "worker_repository_index_services", index_services)
    monkeypatch.setattr(agent_runs, "worker_agent_run_service", agent_service)
    return SimpleNamespace(index=indexing.run_repository_index_task.run,
                           agent=agent_runs.run_agent_task.run, published=published)


def accept_index(context, project_id, headers):
    response = context.client.post(f"/api/v1/projects/{project_id}/index", headers=headers)
    assert response.status_code == 202, response.text
    return response.json()["task_id"]


def ready(resources, workers):
    workers.index(accept_index(resources.context, resources.own, resources.headers))


@pytest.mark.parametrize("victim_state", ["queued", "running"])
@pytest.mark.parametrize("association", ["project", "snapshot"])
def test_invalid_claim_cannot_terminalize_unrelated_run(async_resources, workers, monkeypatch,
                                                     victim_state, association):
    a, r = async_resources, async_resources.r
    context = r.context
    task_id, victim_id = UUID(a.own["task_id"]), UUID(a.other["run_id"])
    with context.uow_factory() as uow, uow.transaction():
        if victim_state == "running":
            uow.agent_runs.mark_running(victim_id)
        victim = uow.agent_runs.get(victim_id)
        victim_task = uow.background_tasks.get(UUID(a.other["task_id"]))
        # Either the Project or just the Snapshot disagrees with the linked Run.
        values = {"agent_run_id": victim_id}
        if association == "project":
            values["snapshot_id"] = victim.snapshot_id
        else:
            values["project_id"] = victim.project_id
        mutate(uow, "background_tasks", BackgroundTaskORM, task_id, **values)

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid association reached repository/artifact/Core access")

    monkeypatch.setattr(context.app.state.workspace, "checkout_exact", forbidden)
    monkeypatch.setattr(context.app.state.agent_workspace, "prepare", forbidden)
    monkeypatch.setattr(context.app.state.adapter, "run_agent", forbidden)
    with pytest.raises(BackendError) as failure:
        workers.agent(str(task_id))
    assert failure.value.code == "background_task_consistency_error"
    with context.uow_factory() as uow, uow.transaction():
        assert uow.agent_runs.get(victim_id) == victim
        assert uow.background_tasks.get(victim_task.id) == victim_task
        assert uow.trace_events.list_for_run(victim_id) == []
        invalid = uow.background_tasks.get(task_id)
        assert invalid.status == "failed" and invalid.attempt_count == 1
    assert workers.published == []


@pytest.mark.parametrize("kind", ["agent", "index"])
def test_foreign_snapshot_cannot_be_executed_or_recovered(resources, workers, monkeypatch, kind):
    r, context = resources, resources.context
    if kind == "agent":
        ready(r, workers)
        response = submit(context, r.own, r.headers)
        assert response.status_code == 202, response.text
        data = response.json()
        task_id = UUID(data["task_id"])
    else:
        task_id = UUID(accept_index(context, r.own, r.headers))
    with context.uow_factory() as uow, uow.transaction():
        index_path, graph_path = context.app.state.artifacts.relative_paths(r.foreign_snapshot.id)
        foreign = uow.snapshots.upsert(
            r.foreign, r.foreign_snapshot.commit_sha, index_artifact_path=index_path,
            graph_artifact_path=graph_path, summary=IndexSummary(1, 1, 0),
        )
        assert foreign.status == "ready"
        mutate(uow, "background_tasks", BackgroundTaskORM, task_id, snapshot_id=foreign.id)
        if kind == "agent":
            mutate(uow, "agent_runs", AgentRunORM, UUID(data["run_id"]), snapshot_id=foreign.id)

    def forbidden(*args, **kwargs):
        pytest.fail("Foreign Snapshot reached workspace or artifact access")

    for target, name in [(context.app.state.workspace, "checkout_exact"),
                         (context.app.state.workspace, "commit_available"),
                         (context.app.state.artifacts, "validate"),
                         (context.app.state.artifacts, "recover_published")]:
        monkeypatch.setattr(target, name, forbidden)
    with pytest.raises(BackendError) as failure:
        getattr(workers, kind)(str(task_id))
    assert failure.value.code == ("project_not_ready" if kind == "agent" else "database_conflict")
    with context.uow_factory() as uow, uow.transaction():
        assert uow.snapshots.get(foreign.id) == foreign
        assert uow.background_tasks.get(task_id).status == "failed"


@pytest.mark.parametrize("kind", ["agent", "index"])
def test_disabled_owner_blocks_new_http_but_accepted_worker_needs_no_jwt(
    resources, workers, monkeypatch, kind,
):
    r, context = resources, resources.context
    if kind == "agent":
        ready(r, workers)
        response = submit(context, r.own, r.headers)
        assert response.status_code == 202, response.text
        task_id = response.json()["task_id"]
    else:
        task_id = accept_index(context, r.own, r.headers)
    with context.uow_factory() as uow, uow.transaction():
        mutate(uow, "users", UserORM, r.owner, status=UserStatus.DISABLED)
    denied = context.client.post(f"/api/v1/projects/{r.own}/index", headers=r.headers)
    assert denied.status_code == 401
    monkeypatch.setattr(context.app.state, "auth_settings", None)
    monkeypatch.setenv("PYCODE_JWT_SECRET", "")
    # Execute and redeliver the actual Celery adapter with only a durable Task ID.
    getattr(workers, kind)(task_id)
    getattr(workers, kind)(task_id)
    with context.uow_factory() as uow, uow.transaction():
        task = uow.background_tasks.get(UUID(task_id))
        assert task.status == "completed" and task.attempt_count == 1
        assert task.snapshot_id == r.snapshot.id
        if kind == "agent":
            assert uow.agent_runs.get(task.agent_run_id).status == "completed"
            trace = uow.trace_events.list_for_run(task.agent_run_id)
            assert trace
            assert all(r.headers["Authorization"].split()[1] not in str(event) for event in trace)


def test_deletion_is_restricted_or_orphaned_task_fails_before_io(resources, workers, monkeypatch):
    r, context = resources, resources.context
    task_id = accept_index(context, r.own, r.headers)
    with context.uow_factory() as uow:
        if not isinstance(uow, InMemoryUnitOfWork):
            for model, record_id in [(ProjectORM, r.own), (UserORM, r.owner)]:
                with pytest.raises(BackendError) as failure, uow.transaction():
                    uow.session.execute(delete(model).where(model.id == record_id))
                assert failure.value.code == "database_conflict"
            with uow.transaction():
                assert uow.projects.get(r.own).owner_id == r.owner
                assert uow.background_tasks.get(UUID(task_id)).status == "queued"
            return
        # The in-memory corruption seam also verifies the missing-resource path.
        with uow.transaction():
            del uow.state.projects[r.own]

    def forbidden(*args, **kwargs):
        pytest.fail("Deleted Project reached workspace or artifact access")

    monkeypatch.setattr(context.app.state.workspace, "synchronize", forbidden)
    monkeypatch.setattr(context.app.state.artifacts, "build_and_publish", forbidden)
    with pytest.raises(BackendError) as failure:
        workers.index(task_id)
    assert failure.value.code == "project_not_found"
    with context.uow_factory() as uow, uow.transaction():
        assert uow.background_tasks.get(UUID(task_id)).status == "failed"


def test_cross_project_workspace_path_is_rejected_before_checkout(resources, workers, monkeypatch):
    r, context = resources, resources.context
    ready(r, workers)
    response = submit(context, r.own, r.headers)
    assert response.status_code == 202, response.text
    foreign_repo = context.app.state.workspace.path_for(r.foreign)
    foreign_repo.mkdir(parents=True)
    private = foreign_repo / "private.py"
    private.write_text("private = 42\n", encoding="utf-8")
    with context.uow_factory() as uow, uow.transaction():
        mutate(uow, "projects", ProjectORM, r.own, workspace_path=str(foreign_repo))

    def forbidden(*args, **kwargs):
        pytest.fail("Foreign workspace reached Git checkout or Core")

    monkeypatch.setattr(context.app.state.workspace.source, "checkout_exact", forbidden)
    monkeypatch.setattr(context.app.state.adapter, "run_agent", forbidden)
    with pytest.raises(BackendError) as failure:
        workers.agent(response.json()["task_id"])
    assert failure.value.code == "workspace_unavailable"
    assert private.read_text(encoding="utf-8") == "private = 42\n"
    assert not (foreign_repo / ".pclens").exists()


def test_two_users_concurrent_workers_isolate_artifacts_and_trace(resources, workers, monkeypatch):
    r, context = resources, resources.context
    state = context.app.state
    pairs = [(r.own, r.headers, r.snapshot.id),
             (r.foreign, r.other_headers, r.foreign_snapshot.id)]
    original_clone, original_index = state.workspace.source.clone, state.adapter.index
    index_barrier = Barrier(2)

    def clone(repo_url, branch, destination, *, project_id=None):
        revision = original_clone(repo_url, branch, destination, project_id=project_id)
        (destination / f"tenant_{project_id.hex}.py").write_text("value = 42\n", encoding="utf-8")
        return revision

    def index(*args, **kwargs):
        index_barrier.wait(timeout=30)
        return original_index(*args, **kwargs)

    monkeypatch.setattr(state.workspace.source, "clone", clone)
    monkeypatch.setattr(state.adapter, "index", index)
    task_ids = [accept_index(context, project_id, headers) for project_id, headers, _ in pairs]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(workers.index, task_ids))

    submissions = []
    for i, (project_id, headers, snapshot_id) in enumerate(pairs):
        response = submit(context, project_id, headers, task=f"explain tenant_{project_id.hex}.py")
        assert response.status_code == 202, response.text
        submissions.append(response.json())
        with context.uow_factory() as uow, uow.transaction():
            task = uow.background_tasks.get(UUID(task_ids[i]))
            assert task.status == "completed" and task.snapshot_id == snapshot_id
            snapshot = uow.snapshots.get(snapshot_id)
        bundle = state.artifacts.validate(project_id, snapshot)
        files = {file.path for file in load_index(bundle.index_path).files}
        assert f"tenant_{project_id.hex}.py" in files
        assert f"tenant_{pairs[1-i][0].hex}.py" not in files

    original_agent, agent_barrier = state.adapter.run_agent, Barrier(2)

    def agent(root, task, *, trace_observer=None):
        agent_barrier.wait(timeout=30)
        return original_agent(root, task, trace_observer=trace_observer)

    monkeypatch.setattr(state.adapter, "run_agent", agent)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(workers.agent, [data["task_id"] for data in submissions]))
    for i, (project_id, headers, snapshot_id) in enumerate(pairs):
        run_id = UUID(submissions[i]["run_id"])
        with context.uow_factory() as uow, uow.transaction():
            task = uow.background_tasks.get(UUID(submissions[i]["task_id"]))
            run = uow.agent_runs.get(run_id)
            assert task.status == run.status == "completed"
            assert task.project_id == run.project_id == project_id
            assert task.snapshot_id == run.snapshot_id == snapshot_id
            trace = uow.trace_events.list_for_run(run_id)
        assert trace
        realtime = [event for owner, event in workers.published if owner == run_id]
        assert realtime and realtime[-1].event_type == "run_completed"
        assert [event.sequence for event in realtime] == list(range(len(realtime)))
        for event in [*trace, *realtime]:
            assert str(pairs[1-i][2]) not in str(event)
            assert f"tenant_{pairs[1-i][0].hex}" not in str(event)
            assert all(h["Authorization"].split()[1] not in str(event) for _, h, _ in pairs)
        materialized = load_index(state.workspace.path_for(project_id) / ".pclens" / "index.json")
        files = {file.path for file in materialized.files}
        assert f"tenant_{project_id.hex}.py" in files
        assert f"tenant_{pairs[1-i][0].hex}.py" not in files
        # The same records remain object-authorized after concurrent completion.
        other_headers = pairs[1-i][1]
        for url in [f"/api/v1/tasks/{task.id}", f"/api/v1/agent-runs/{run_id}/trace"]:
            assert context.client.get(url, headers=headers).status_code == 200
            assert context.client.get(url, headers=other_headers).status_code == 404
