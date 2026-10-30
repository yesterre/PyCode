from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from threading import Event
from uuid import UUID, uuid4

import jwt
import pytest

from backend.app.core.errors import BackendError
from backend.app.core.models import UserStatus
from backend.app.db.models import UserORM
from backend.app.services.background_tasks import BackgroundTaskService
from test_b01_users import uow_factory
from test_b02_auth import auth_context
from test_b02_security import JWT_SECRET, claims_for
from test_b03_project_ownership import assert_unauthenticated
from test_b04_project_derived_auth import project_service, resources
from test_b05_async_auth import counts, submit
from test_b07_worker_isolation import accept_index, mutate, ready, workers
from test_postgresql import postgresql_engine


def archive(r, archived=True, *, project=None, headers=None):
    return r.context.client.post(
        f"/api/v1/projects/{project or r.own}/{'archive' if archived else 'restore'}",
        headers=r.headers if headers is None else headers,
    )


def listed(r, **params):
    response = r.context.client.get("/api/v1/projects", headers=r.headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def timestamps(payload):
    # PostgreSQL can serialize the same instant using its session timezone.
    return {key: datetime.fromisoformat(value) if key in {"created_at", "updated_at", "archived_at"}
            and value is not None else value for key, value in payload.items()}


def test_owner_archive_restore_are_idempotent_and_lists_filter_before_pagination(resources):
    r = resources
    original = r.context.client.get(f"/api/v1/projects/{r.own}", headers=r.headers).json()
    assert original["archived_at"] is None
    first = archive(r)
    assert first.status_code == 200, first.text
    saved = first.json()
    assert datetime.fromisoformat(saved["archived_at"]).utcoffset() is not None
    assert saved["status"] == original["status"]
    assert timestamps(archive(r).json()) == timestamps(saved)
    assert [p["id"] for p in listed(r)["projects"]] == [str(r.sibling)]
    assert [p["id"] for p in listed(r, archive_status="archived")["projects"]] == [str(r.own)]
    assert archive(r, project=r.sibling).status_code == 200
    assert listed(r)["projects"] == []
    page = listed(r, archive_status="archived", limit=1)
    assert page["has_more"] and len(page["projects"]) == 1
    second = listed(r, archive_status="archived", limit=1, offset=1)
    assert not second["has_more"]
    assert {page["projects"][0]["id"], second["projects"][0]["id"]} == {str(r.own), str(r.sibling)}
    assert {p["id"] for p in listed(r, archive_status="all")["projects"]} == {str(r.own), str(r.sibling)}
    restored = archive(r, False)
    assert restored.status_code == 200 and restored.json()["archived_at"] is None
    assert timestamps(archive(r, False).json()) == timestamps(restored.json())
    assert [p["id"] for p in listed(r)["projects"]] == [str(r.own)]
    assert r.context.client.get("/api/v1/projects", headers=r.headers,
                                params={"archive_status": "unknown"}).status_code == 422


def test_archive_authorization_hides_foreign_legacy_and_missing_projects(resources):
    r = resources
    with r.context.uow_factory() as uow, uow.transaction():
        before = uow.projects.list_page(100, 0)
    for archived in [True, False]:
        for project in [r.foreign, r.legacy, uuid4()]:
            response = archive(r, archived, project=project)
            assert response.status_code == 404
            assert response.json()["detail"]["code"] == "project_not_found"
        assert archive(r, archived, project="invalid-uuid").status_code == 422
    with r.context.uow_factory() as uow, uow.transaction():
        assert uow.projects.list_page(100, 0) == before


@pytest.mark.parametrize("mode", ["missing", "malformed", "expired", "disabled"])
def test_archive_and_restore_require_verified_identity(resources, mode):
    r, headers = resources, {}
    if mode == "malformed":
        headers = {"Authorization": "Bearer invalid"}
    elif mode == "expired":
        claims = claims_for(r.owner)
        claims.update(iat=1, nbf=1, exp=2)
        headers = {"Authorization": "Bearer " + jwt.encode(claims, JWT_SECRET, algorithm="HS256")}
    elif mode == "disabled":
        headers = r.headers
        with r.context.uow_factory() as uow, uow.transaction():
            mutate(uow, "users", UserORM, r.owner, status=UserStatus.DISABLED)
    for archived in [True, False]:
        assert_unauthenticated(archive(r, archived, headers=headers))
    with r.context.uow_factory() as uow, uow.transaction():
        assert uow.projects.get(r.own).archived_at is None


def test_archived_project_rejects_new_work_without_committed_records_or_side_effects(
    resources, workers, monkeypatch,
):
    r, context = resources, resources.context
    ready(r, workers)
    assert archive(r).status_code == 200
    before = counts(context)
    state = context.app.state
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        pytest.fail("Archived submission reached Git, analysis, artifacts, or the broker")

    for target, methods in [(state.workspace, ["synchronize", "checkout_exact"]),
                            (state.adapter, ["ask", "impact", "run_agent"]),
                            (state.artifacts, ["build_and_publish"]),
                            (state.task_dispatcher, ["enqueue_repository_index", "enqueue_agent_run"])]:
        for method in methods:
            monkeypatch.setattr(target, method, forbidden)
    responses = [context.client.post(f"/api/v1/projects/{r.own}/index", headers=r.headers),
                 submit(context, r.own, r.headers),
                 context.client.post(f"/api/v1/projects/{r.own}/ask", headers=r.headers,
                                     json={"question": "entry"}),
                 context.client.post(f"/api/v1/projects/{r.own}/impact", headers=r.headers,
                                     json={"file_path": "main.py"})]
    assert all(response.status_code == 409 for response in responses)
    assert all(response.json()["detail"]["code"] == "project_archived" for response in responses)
    assert counts(context) == before and calls == []
    # The trusted submission helper also cannot create NEW archived work.
    with context.uow_factory() as uow, pytest.raises(BackendError) as failure:
        BackgroundTaskService(uow, state.task_dispatcher).create_repository_index_task(r.own)
    assert failure.value.code == "project_archived" and counts(context) == before


def test_accepted_work_replays_and_executes_after_archive_and_evidence_stays_readable(resources, workers):
    r, context = resources, resources.context
    ready(r, workers)
    indexed = context.client.post(f"/api/v1/projects/{r.own}/index",
                                 headers={**r.headers, "Idempotency-Key": "accepted-index"})
    agent = submit(context, r.own, r.headers, "accepted-agent")
    assert indexed.status_code == agent.status_code == 202
    # Read through the UoW in both implementations, keeping immutable evidence.
    with context.uow_factory() as uow, uow.transaction():
        snapshot = uow.snapshots.get(r.snapshot.id)
    bundle = context.app.state.artifacts.read_ready(r.own, snapshot)
    before = bundle.index_path.read_bytes(), bundle.graph_path.read_bytes()
    saved = archive(r).json()["archived_at"]
    count_before = counts(context)
    dispatches = list(context.app.state.task_dispatcher.repository_index_task_ids), list(
        context.app.state.task_dispatcher.agent_run_task_ids)
    replay = context.client.post(f"/api/v1/projects/{r.own}/index",
                                headers={**r.headers, "Idempotency-Key": "accepted-index"})
    assert replay.status_code == 202 and replay.json() == indexed.json()
    replay_agent = submit(context, r.own, r.headers, "accepted-agent")
    assert replay_agent.status_code == 202 and replay_agent.json() == agent.json()
    assert submit(context, r.own, r.headers, "accepted-agent", task="changed").status_code == 409
    assert counts(context) == count_before
    assert dispatches == (context.app.state.task_dispatcher.repository_index_task_ids,
                          context.app.state.task_dispatcher.agent_run_task_ids)
    workers.index(indexed.json()["task_id"])
    workers.agent(agent.json()["task_id"])
    base = f"/api/v1/projects/{r.own}"
    urls = [base, base + "/analysis-history", base + "/snapshots",
            base + f"/snapshots/{snapshot.id}", base + f"/snapshots/{snapshot.id}/graph?q=main",
            f"/api/v1/tasks/{agent.json()['task_id']}",
            f"/api/v1/agent-runs/{agent.json()['run_id']}",
            f"/api/v1/agent-runs/{agent.json()['run_id']}/trace",
            f"/api/v1/agent-runs/{agent.json()['run_id']}/events"]
    for url in urls:
        response = context.client.get(url, headers=r.headers)
        assert response.status_code == 200, (url, response.text)
        assert context.client.get(url, headers=r.other_headers).status_code == 404
    with context.uow_factory() as uow, uow.transaction():
        assert uow.background_tasks.get(UUID(indexed.json()["task_id"])).status == "completed"
        assert uow.agent_runs.get(UUID(agent.json()["run_id"])).status == "completed"
        assert uow.projects.get(r.own).archived_at == datetime.fromisoformat(saved)
    assert before == (bundle.index_path.read_bytes(), bundle.graph_path.read_bytes())
    assert archive(r, False).status_code == 200
    assert submit(context, r.own, r.headers).status_code == 202


@pytest.mark.parametrize("kind", ["index", "agent", "ask"])
def test_archive_during_execution_keeps_work_and_archive_state(resources, workers, monkeypatch, kind):
    r, context = resources, resources.context
    if kind != "index":
        ready(r, workers)
    if kind == "index":
        task_id = accept_index(context, r.own, r.headers)
        action = lambda: workers.index(task_id)
    elif kind == "agent":
        task_id = submit(context, r.own, r.headers).json()["task_id"]
        action = lambda: workers.agent(task_id)
    else:
        action = lambda: context.client.post(f"/api/v1/projects/{r.own}/ask", headers=r.headers,
                                             json={"question": "entry"})
    entered, release = Event(), Event()
    method = {"index": "index", "agent": "run_agent", "ask": "ask"}[kind]
    original = getattr(context.app.state.adapter, method)

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(30)
        return original(*args, **kwargs)

    monkeypatch.setattr(context.app.state.adapter, method, blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(action)
        try:
            assert entered.wait(30)
            archived = archive(r)
            assert archived.status_code == 200, archived.text
        finally:
            release.set()
        result = future.result(timeout=30)
    if kind == "ask":
        assert result.status_code == 200, result.text
    with context.uow_factory() as uow, uow.transaction():
        assert uow.projects.get(r.own).archived_at is not None
        if kind != "ask":
            assert uow.background_tasks.get(UUID(task_id)).status == "completed"


@pytest.mark.parametrize("first", ["archive", "submit"])
@pytest.mark.parametrize("kind", ["index", "agent"])
def test_archive_and_admission_serialize_in_the_same_transaction(resources, workers, monkeypatch, first, kind):
    r, context = resources, resources.context
    ready(r, workers)
    with context.uow_factory() as uow:
        repository_type = type(uow.projects)
    original = repository_type.get_owned
    entered, release, attempted = Event(), Event(), Event()

    def held(repository, *args, **kwargs):
        project = original(repository, *args, **kwargs)
        if kwargs.get("for_update") and not entered.is_set():
            entered.set()
            assert release.wait(30)
        return project

    monkeypatch.setattr(repository_type, "get_owned", held)

    def perform(action):
        with context.uow_factory() as uow:
            if action == "archive":
                return project_service(context, uow).set_archived(r.own, r.owner, archived=True)
            background = BackgroundTaskService(uow, context.app.state.task_dispatcher)
            if kind == "index":
                return background.create_owned_repository_index_task(r.own, r.owner)
            from backend.app.runtime import build_agent_run_service

            return build_agent_run_service(
                uow, background_tasks=background, projects=project_service(context, uow),
                adapter=context.app.state.adapter, agent_workspace=context.app.state.agent_workspace,
            ).submit(r.own, "entry", owner_id=r.owner)

    second = "submit" if first == "archive" else "archive"

    def contender():
        attempted.set()
        return perform(second)

    with ThreadPoolExecutor(max_workers=2) as pool:
        leading = pool.submit(perform, first)
        try:
            assert entered.wait(30)
            trailing = pool.submit(contender)
            assert attempted.wait(30) and not trailing.done()
        finally:
            release.set()
        leading.result(timeout=30)
        if first == "archive":
            with pytest.raises(BackendError) as failure:
                trailing.result(timeout=30)
            assert failure.value.code == "project_archived"
        else:
            trailing.result(timeout=30)
    with context.uow_factory() as uow, uow.transaction():
        assert uow.projects.get(r.own).archived_at is not None


def test_archive_between_sync_analysis_checks_prevents_run_creation(resources, workers, monkeypatch):
    r, context = resources, resources.context
    ready(r, workers)
    before = counts(context)
    entered, release = Event(), Event()
    operations = context.app.state.project_operations
    original = operations.operation

    @contextmanager
    def delayed(project_id):
        entered.set()
        assert release.wait(30)
        with original(project_id) as lock:
            yield lock

    monkeypatch.setattr(operations, "operation", delayed)

    def analyze():
        with context.uow_factory() as uow:
            return project_service(context, uow).ask(r.own, "entry", None, owner_id=r.owner)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(analyze)
        try:
            assert entered.wait(30)
            assert archive(r).status_code == 200
        finally:
            release.set()
        with pytest.raises(BackendError) as failure:
            future.result(timeout=30)
    assert failure.value.code == "project_archived" and counts(context) == before


def test_archive_transaction_rolls_back_without_changing_project_or_snapshot(resources):
    r = resources
    with r.context.uow_factory() as uow:
        with uow.transaction():
            before = uow.projects.get(r.own)
        with pytest.raises(RuntimeError, match="rollback"), uow.transaction():
            uow.projects.set_archived(r.own, r.owner, archived=True)
            raise RuntimeError("rollback")
        with uow.transaction():
            assert uow.projects.get(r.own) == before
            assert uow.snapshots.get(r.snapshot.id) == r.snapshot
