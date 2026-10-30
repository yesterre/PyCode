from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import jwt
import pytest
from sqlalchemy import update

from backend.app.core.errors import BackendError
from backend.app.core.models import ProjectStatus, UserStatus
from backend.app.db.models import ProjectORM, UserORM
from backend.app.repositories import InMemoryUnitOfWork
from backend.app.runtime import build_project_service
from backend.app.services.background_tasks import BackgroundTaskService
from test_b01_users import uow_factory
from test_b02_auth import auth_context
from test_b02_security import JWT_SECRET, claims_for
from test_b03_project_ownership import actor, assert_unauthenticated, create
from test_postgresql import postgresql_engine


def project_service(context, uow):
    return build_project_service(
        uow, adapter=context.app.state.adapter, workspace=context.app.state.workspace,
        artifacts=context.app.state.artifacts, operations=context.app.state.project_operations,
    )


def execute_index(context, task_id):
    # Exercise the existing trusted worker service without any browser identity.
    with context.uow_factory() as uow:
        return BackgroundTaskService(uow).execute_repository_index(
            UUID(task_id), project_service(context, uow).index,
        )


@pytest.fixture
def resources(auth_context):
    context = auth_context
    alice, a = actor(context, "alice")
    bob, b = actor(context, "bob")
    own, sibling, foreign = create(context, a), create(context, a), create(context, b)
    with context.uow_factory() as uow, uow.transaction():
        legacy = uow.projects.create("legacy", own["repo_url"])
        projects = [UUID(own["id"]), UUID(sibling["id"]), UUID(foreign["id"]), legacy.id]
        snapshots = [uow.snapshots.start(project_id, "a" * 40) for project_id in projects]
        history = []
        for project_id in projects:
            run = uow.agent_runs.create(project_id, None, task="private history", run_type="ask", model=None)
            history.append(uow.agent_runs.complete(run.id, "private answer"))
    return SimpleNamespace(
        context=context, owner=alice, headers=a, other_owner=bob, other_headers=b,
        own=projects[0], sibling=projects[1], foreign=projects[2], legacy=legacy.id,
        snapshot=snapshots[0], sibling_snapshot=snapshots[1], foreign_snapshot=snapshots[2],
        history=history,
    )


def routes(project_id, snapshot_id):
    base = f"/api/v1/projects/{project_id}"
    return [
        ("post", base + "/index", {}),
        ("post", base + "/ask", {"json": {"question": "entry"}}),
        ("post", base + "/impact", {"json": {"file_path": "main.py"}}),
        ("get", base + "/analysis-history", {}),
        ("get", base + "/snapshots", {}),
        ("get", base + f"/snapshots/{snapshot_id}", {}),
        ("get", base + f"/snapshots/{snapshot_id}/graph", {"params": {"q": "main"}}),
    ]


def guard_derived_access(context, monkeypatch):
    """Fail if a denied request reaches protected reads or side effects."""
    calls = []

    def forbidden(*args, **kwargs):
        calls.append("protected operation")
        raise AssertionError("Denied request reached derived data or an expensive operation")

    with context.uow_factory() as uow:
        for repository, methods in [
            (uow.snapshots, ["get", "get_for_project", "list_recent"]),
            (uow.agent_runs, ["create", "list_history_for_project"]),
            (uow.background_tasks, ["create_submission"]),
        ]:
            for method in methods:
                monkeypatch.setattr(type(repository), method, forbidden)
    state = context.app.state
    for target, methods in [
        (state.project_operations, ["operation"]),
        (state.workspace, ["synchronize", "checkout_exact"]),
        (state.artifacts, ["read_ready", "build_and_publish"]),
        (state.adapter, ["ask", "impact", "index"]),
        (state.task_dispatcher, ["enqueue_repository_index"]),
    ]:
        for method in methods:
            monkeypatch.setattr(target, method, forbidden)
    return calls


def test_all_derived_routes_support_owner_and_preserve_snapshot_and_analysis(resources, llm):
    r, context = resources, resources.context
    accepted = context.client.post(f"/api/v1/projects/{r.own}/index", headers=r.headers)
    assert accepted.status_code == 202, accepted.text
    completed = execute_index(context, accepted.json()["task_id"])
    assert completed.status == "completed" and completed.snapshot_id == r.snapshot.id
    for method, url, kwargs in routes(r.own, r.snapshot.id)[1:]:
        response = context.client.request(method, url, headers=r.headers, **kwargs)
        assert response.status_code == 200, (url, response.text)
        if url.endswith(("/ask", "/impact")):
            assert response.json()["project_id"] == str(r.own)
            assert response.json()["answer"] == "Offline HTTP answer"
        elif url.endswith("/analysis-history"):
            assert len(response.json()["items"]) == 3
            assert {item["id"] for item in response.json()["items"]}.isdisjoint(
                {str(run.id) for run in r.history[1:]},
            )
        elif url.endswith("/snapshots"):
            assert [item["id"] for item in response.json()["snapshots"]] == [str(r.snapshot.id)]
        elif url.endswith("/graph"):
            assert response.json()["snapshot_id"] == str(r.snapshot.id)
            assert response.json()["nodes"]
        else:
            assert response.json()["id"] == str(r.snapshot.id)
            assert response.json()["status"] == "ready"
    assert len(llm.prompts) == 2
    # Changing a query parameter cannot select another user or Snapshot.
    own_snapshots = context.client.get(f"/api/v1/projects/{r.own}/snapshots", headers=r.headers,
        params={"owner_id": str(r.other_owner), "snapshot_id": str(r.foreign_snapshot.id)})
    assert [item["id"] for item in own_snapshots.json()["snapshots"]] == [str(r.snapshot.id)]


@pytest.mark.parametrize("mode", ["missing", "malformed", "signature", "expired", "disabled", "query_token"])
def test_all_derived_routes_require_verified_identity_before_access(resources, monkeypatch, mode, llm):
    r, context = resources, resources.context
    headers, extra = {}, {}
    if mode == "malformed":
        headers = {"Authorization": "Bearer not-a-jwt"}
    elif mode in {"signature", "expired"}:
        claims = claims_for(r.owner)
        if mode == "expired":
            claims.update(iat=1, nbf=1, exp=2)
        token = jwt.encode(claims, "incorrect-test-secret-" * 3 if mode == "signature" else JWT_SECRET,
                           algorithm="HS256")
        headers = {"Authorization": "Bearer " + token}
    elif mode == "disabled":
        headers = r.headers
        with context.uow_factory() as uow, uow.transaction():
            if isinstance(uow, InMemoryUnitOfWork):
                uow.state.users[r.owner] = replace(uow.users.get(r.owner), status=UserStatus.DISABLED)
            else:
                uow.session.execute(update(UserORM).where(UserORM.id == r.owner).values(status="disabled"))
    elif mode == "query_token":
        extra = {"access_token": r.headers["Authorization"].removeprefix("Bearer "), "user_id": str(r.owner)}
    calls = guard_derived_access(context, monkeypatch)
    for method, url, kwargs in routes(r.own, r.snapshot.id):
        kwargs["params"] = {**kwargs.get("params", {}), **extra}
        assert_unauthenticated(context.client.request(method, url, headers=headers, **kwargs))
    assert calls == [] and llm.prompts == []
    assert context.app.state.workspace.source.calls == []


def test_all_derived_routes_hide_foreign_legacy_and_missing_projects_without_effects(resources, monkeypatch):
    r, context = resources, resources.context
    calls = guard_derived_access(context, monkeypatch)
    # Both directions, including a valid Snapshot ID belonging to the path's project.
    for project_id, snapshot_id, headers in [
        (r.foreign, r.foreign_snapshot.id, r.headers),
        (r.own, r.snapshot.id, r.other_headers),
        (r.legacy, uuid4(), r.headers), (uuid4(), uuid4(), r.headers),
    ]:
        for method, url, kwargs in routes(project_id, snapshot_id):
            kwargs["params"] = {**kwargs.get("params", {}), "user_id": str(r.other_owner)}
            response = context.client.request(method, url, headers=headers, **kwargs)
            assert response.status_code == 404, (url, response.text)
            assert response.json() == {"detail": {"code": "project_not_found", "message": "Project does not exist."}}
    assert calls == []
    with context.uow_factory() as uow, uow.transaction():
        for project_id in [r.own, r.sibling, r.foreign, r.legacy]:
            assert uow.projects.get(project_id).status == ProjectStatus.CREATED
            assert len(uow.snapshots.list_for_project(project_id)) == 1
            assert len(uow.agent_runs.list_for_project(project_id)) == 1


def test_snapshot_detail_and_graph_reject_other_projects_even_for_same_owner(resources, monkeypatch):
    r, context = resources, resources.context

    def forbidden(*args, **kwargs):
        raise AssertionError("Must not load an unscoped Snapshot or read foreign artifacts")

    monkeypatch.setattr(context.app.state.artifacts, "read_ready", forbidden)
    with context.uow_factory() as uow:
        monkeypatch.setattr(type(uow.snapshots), "get", forbidden)
    for identifier in [r.foreign_snapshot.id, r.sibling_snapshot.id, uuid4()]:
        for suffix, params in [("", {}), ("/graph", {"q": "main"})]:
            response = context.client.get(f"/api/v1/projects/{r.own}/snapshots/{identifier}{suffix}",
                                          headers=r.headers, params=params)
            assert response.status_code == 404, response.text
            assert response.json() == {"detail": {"code": "snapshot_not_found", "message": "Project snapshot does not exist."}}
        with context.uow_factory() as uow, uow.transaction():
            with pytest.raises(BackendError) as failure:
                uow.snapshots.get_for_project(r.own, identifier)
            assert failure.value.code == "snapshot_not_found"
    own = context.client.get(f"/api/v1/projects/{r.own}/snapshots/{r.snapshot.id}", headers=r.headers)
    assert own.status_code == 200 and own.json()["id"] == str(r.snapshot.id)


def test_service_entry_points_check_owner_without_http(resources, monkeypatch):
    r, context = resources, resources.context
    calls = guard_derived_access(context, monkeypatch)
    with context.uow_factory() as uow:
        service = project_service(context, uow)
        tasks = BackgroundTaskService(uow, context.app.state.task_dispatcher)
        for owner in [r.other_owner, None]:
            operations = [
                lambda: service.list_snapshots(r.own, 20, owner_id=owner),
                lambda: service.get_snapshot(r.own, r.snapshot.id, owner_id=owner),
                lambda: service.get_graph(r.own, r.snapshot.id, owner_id=owner,
                                         q="main", file_path=None, node_type=None, limit=10),
                lambda: service.get_analysis_history(r.own, 20, 0, owner_id=owner),
                lambda: service.ask(r.own, "entry", None, owner_id=owner),
                lambda: service.impact(r.own, "main.py", None, owner_id=owner),
                lambda: tasks.create_owned_repository_index_task(r.own, owner, "private-key"),
            ]
            for operation in operations:
                with pytest.raises(BackendError) as failure:
                    operation()
                assert failure.value.code == "project_not_found"
    assert calls == []


def test_foreign_owner_cannot_replay_index_idempotency_key(resources):
    r, context = resources, resources.context
    url = f"/api/v1/projects/{r.foreign}/index"
    headers = {**r.other_headers, "Idempotency-Key": "victim-index-key"}
    first = context.client.post(url, headers=headers)
    assert first.status_code == 202
    task_id = UUID(first.json()["task_id"])
    for attacker in [{}, r.headers]:
        denied = context.client.post(url, headers={**attacker, "Idempotency-Key": "victim-index-key"})
        assert denied.status_code == (401 if not attacker else 404)
        assert str(task_id) not in denied.text
    replay = context.client.post(url, headers=headers)
    assert replay.status_code == 202 and replay.json() == first.json()
    assert context.app.state.task_dispatcher.repository_index_task_ids == [task_id]
    with context.uow_factory() as uow, uow.transaction():
        task = uow.background_tasks.get(task_id)
        assert task.project_id == r.foreign and task.status == "queued" and task.attempt_count == 0


def test_sync_analysis_rejects_forged_identity_and_snapshot_body_fields(resources):
    r, context = resources, resources.context
    for field in ["owner_id", "user_id", "snapshot_id"]:
        for suffix, body in [("ask", {"question": "entry"}), ("impact", {"file_path": "main.py"})]:
            response = context.client.post(f"/api/v1/projects/{r.own}/{suffix}", headers=r.headers,
                json={**body, field: str(r.foreign_snapshot.id)})
            assert response.status_code == 422, response.text
    with context.uow_factory() as uow, uow.transaction():
        assert len(uow.agent_runs.list_for_project(r.own)) == 1


def test_sync_analysis_fails_closed_if_current_snapshot_points_to_another_project(resources):
    r, context = resources, resources.context
    with context.uow_factory() as uow, uow.transaction():
        if isinstance(uow, InMemoryUnitOfWork):
            uow.state.projects[r.own] = replace(uow.projects.get(r.own), current_snapshot_id=r.foreign_snapshot.id)
        else:
            uow.session.execute(update(ProjectORM).where(ProjectORM.id == r.own)
                                .values(current_snapshot_id=r.foreign_snapshot.id))
    for suffix, body in [("ask", {"question": "entry"}), ("impact", {"file_path": "main.py"})]:
        response = context.client.post(f"/api/v1/projects/{r.own}/{suffix}", headers=r.headers, json=body)
        assert response.status_code == 404 and response.json()["detail"]["code"] == "snapshot_not_found"
    with context.uow_factory() as uow, uow.transaction():
        assert len(uow.agent_runs.list_for_project(r.own)) == 1
    assert context.app.state.workspace.source.calls == []


def test_owned_unready_snapshot_and_analysis_failure_keep_existing_behavior(resources, monkeypatch):
    r, context = resources, resources.context
    graph = f"/api/v1/projects/{r.own}/snapshots/{r.snapshot.id}/graph"
    assert context.client.get(graph, headers=r.headers, params={"q": "main"}).status_code == 409
    url = f"/api/v1/projects/{r.own}"
    accepted = context.client.post(url + "/index", headers=r.headers)
    assert accepted.status_code == 202
    execute_index(context, accepted.json()["task_id"])

    def crash(*args, **kwargs):
        raise RuntimeError("private analysis failure")

    monkeypatch.setattr(context.app.state.adapter, "ask", crash)
    response = context.client.post(url + "/ask", headers=r.headers, json={"question": "entry"})
    assert response.status_code == 500 and "private analysis failure" not in response.text
    with context.uow_factory() as uow, uow.transaction():
        assert uow.projects.get(r.own).current_snapshot_id == r.snapshot.id
        assert uow.projects.get(r.own).status == ProjectStatus.READY
        assert sorted(str(run.status) for run in uow.agent_runs.list_for_project(r.own)) == ["completed", "failed"]


def test_openapi_protects_every_project_route(auth_context):
    schema = auth_context.client.get("/openapi.json").json()
    for path, methods in schema["paths"].items():
        if path.startswith("/api/v1/projects"):
            for operation in methods.values():
                assert operation["security"] == [{"BearerAuth": []}]
                assert "401" in operation["responses"]
