from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import jwt
import pytest
from sqlalchemy import func, select, update

from backend.app.core.application_trace import ApplicationEventType, ApplicationTraceEvent
from backend.app.core.errors import BackendError
from backend.app.core.models import IndexSummary, UserStatus
from backend.app.db.models import AgentRunORM, BackgroundTaskORM, TraceEventORM, UserORM
from backend.app.repositories import InMemoryUnitOfWork
from backend.app.runtime import build_agent_run_service
from backend.app.services.background_tasks import BackgroundTaskService
from test_b01_users import uow_factory
from test_b02_auth import auth_context
from test_b02_security import JWT_SECRET, claims_for
from test_b03_project_ownership import assert_unauthenticated
from test_b04_project_derived_auth import execute_index, project_service, resources
from test_postgresql import postgresql_engine


ROOT = "/api/v1/agent-runs"


def submit(context, project_id, headers, key=None, task="private task"):
    return context.client.post(ROOT, json={"project_id": str(project_id), "task": task},
        headers={**headers, **({"Idempotency-Key": key} if key is not None else {})})


@pytest.fixture
def async_resources(resources):
    r, context = resources, resources.context
    with context.uow_factory() as uow, uow.transaction():
        for project_id in [r.own, r.sibling, r.foreign, r.legacy]:
            snapshot = uow.snapshots.get_by_commit(project_id, "a" * 40)
            index_path, graph_path = context.app.state.artifacts.relative_paths(snapshot.id)
            uow.snapshots.upsert(project_id, "a" * 40, index_artifact_path=index_path,
                                graph_artifact_path=graph_path, summary=IndexSummary(1, 1, 0))
            uow.projects.set_current_snapshot(project_id, snapshot.id)
        legacy_run = uow.agent_runs.create_queued(r.legacy, None, task="legacy private", run_type="agent", model=None)
        legacy_task = uow.background_tasks.create("agent_run", project_id=r.legacy, agent_run_id=legacy_run.id)
        health = uow.background_tasks.create("health")
    own = submit(context, r.own, r.headers, "owner-key")
    other = submit(context, r.foreign, r.other_headers, "other-key")
    assert own.status_code == other.status_code == 202, (own.text, other.text)
    return SimpleNamespace(r=r, own=own.json(), other=other.json(), legacy_run=legacy_run,
                           legacy_task=legacy_task, health=health)


def counts(context):
    with context.uow_factory() as uow, uow.transaction():
        if isinstance(uow, InMemoryUnitOfWork):
            return tuple(len(getattr(uow.state, name)) for name in ["background_tasks", "agent_runs", "trace_events"])
        return tuple(uow.session.scalar(select(func.count()).select_from(model))
                     for model in [BackgroundTaskORM, AgentRunORM, TraceEventORM])


def reads(submission):
    return [f"/api/v1/tasks/{submission['task_id']}", f"{ROOT}/{submission['run_id']}",
            f"{ROOT}/{submission['run_id']}/trace"]


def forbidden_access(context, monkeypatch, *, submission=False):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append("unexpected access")
        raise AssertionError("Denied request reached private records or side effects")

    with context.uow_factory() as uow:
        for repository, methods in [
            (uow.agent_runs, ["get"] + (["create_queued"] if submission else [])),
            (uow.background_tasks, ["get"] + (["create_submission"] if submission else [])),
            (uow.trace_events, ["list_for_run"]),
        ]:
            for method in methods:
                monkeypatch.setattr(type(repository), method, forbidden)
    for target, name in [(context.app.state.adapter, "run_agent"),
                         (context.app.state.task_dispatcher, "enqueue_agent_run"),
                         (context.app.state.agent_workspace, "prepare")]:
        monkeypatch.setattr(target, name, forbidden)
    return calls


@pytest.mark.parametrize("state", ["queued", "running", "completed", "failed"])
def test_task_run_and_trace_authorize_both_directions_in_every_state(async_resources, monkeypatch, state):
    a, r = async_resources, async_resources.r
    context = r.context
    with context.uow_factory() as uow, uow.transaction():
        for data in [a.own, a.other]:
            run_id, task_id = UUID(data["run_id"]), UUID(data["task_id"])
            if state != "queued":
                task = uow.background_tasks.claim(task_id).task
                run = uow.agent_runs.mark_running(run_id)
                if state == "completed":
                    uow.agent_runs.complete(run_id, "private result")
                    uow.background_tasks.mark_completed(task_id, snapshot_id=run.snapshot_id,
                                                       execution_token=task.execution_token)
                elif state == "failed":
                    uow.agent_runs.fail(run_id, "safe failure")
                    uow.background_tasks.mark_failed(task_id, error_code="agent_run_failed",
                        error_message="safe failure", execution_token=task.execution_token)
            event = ApplicationTraceEvent(0, ApplicationEventType.RUN_STARTED,
                datetime.now(timezone.utc), None,
                {"run_type": "agent", "snapshot_id": str(uow.agent_runs.get(run_id).snapshot_id)})
            uow.trace_events.create(run_id, 0, event.event_type.value, payload=event.database_payload())
    for data, headers in [(a.own, r.headers), (a.other, r.other_headers)]:
        for path in reads(data)[:2]:
            response = context.client.get(path, headers=headers)
            assert response.status_code == 200 and response.json()["status"] == state
        trace = context.client.get(reads(data)[2], headers=headers)
        assert trace.status_code == 200
        assert len(trace.json()["events"]) == (1 if state in {"completed", "failed"} else 0)
    before = counts(context)
    calls = forbidden_access(context, monkeypatch)
    for data, attacker in [(a.other, r.headers), (a.own, r.other_headers)]:
        for path in reads(data):
            denied = context.client.get(path, headers=attacker, params={"owner_id": str(r.other_owner)})
            assert denied.status_code == 404, denied.text
            code = "background_task_not_found" if "/tasks/" in path else "agent_run_not_found"
            missing_id = str(uuid4())
            missing_path = (f"/api/v1/tasks/{missing_id}" if "/tasks/" in path else
                            f"{ROOT}/{missing_id}" + ("/trace" if path.endswith("/trace") else ""))
            missing = context.client.get(missing_path, headers=attacker)
            assert missing.status_code == 404 and missing.json() == denied.json()
            assert denied.json()["detail"]["code"] == code
    assert calls == [] and counts(context) == before


@pytest.mark.parametrize("mode", ["missing", "malformed", "signature", "expired", "disabled", "query_token"])
def test_all_async_entry_points_require_verified_active_identity(async_resources, monkeypatch, mode):
    a, r = async_resources, async_resources.r
    context, headers, params = r.context, {}, {}
    if mode == "malformed":
        headers = {"Authorization": "Bearer bad-token"}
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
        params = {"access_token": r.headers["Authorization"].removeprefix("Bearer "), "user_id": str(r.owner)}
    before = counts(context)
    calls = forbidden_access(context, monkeypatch, submission=True)
    for path in reads(a.own):
        assert_unauthenticated(context.client.get(path, headers=headers, params=params))
    assert_unauthenticated(context.client.post(ROOT, headers=headers, params=params,
        json={"project_id": str(r.own), "task": "private task"}))
    assert counts(context) == before and calls == []


def test_legacy_and_unbound_internal_records_are_invisible_to_all_users(async_resources):
    a, r = async_resources, async_resources.r
    for owner, headers in [(r.owner, r.headers), (r.other_owner, r.other_headers)]:
        for data in [
            {"run_id": str(a.legacy_run.id), "task_id": str(a.legacy_task.id)},
            {"run_id": str(uuid4()), "task_id": str(a.health.id)},
        ]:
            for path in reads(data):
                assert r.context.client.get(path, headers=headers).status_code == 404
        with r.context.uow_factory() as uow, uow.transaction():
            for repository, identifier in [(uow.agent_runs, a.legacy_run.id),
                                          (uow.background_tasks, a.legacy_task.id),
                                          (uow.background_tasks, a.health.id)]:
                with pytest.raises(BackendError):
                    repository.get_owned(identifier, owner)
    assert r.context.client.post("/api/v1/tasks/health").status_code == 405
    assert r.context.client.post("/api/v1/tasks/health", headers=r.headers).status_code == 405


def test_submit_rejects_foreign_legacy_and_missing_projects_before_idempotency(async_resources, monkeypatch):
    a, r = async_resources, async_resources.r
    before = counts(r.context)
    calls = forbidden_access(r.context, monkeypatch, submission=True)
    for project_id, headers in [(r.foreign, r.headers), (r.own, r.other_headers),
                                (r.legacy, r.headers), (uuid4(), r.headers)]:
        for text in ["private task", "different private task"]:
            response = submit(r.context, project_id, headers, "other-key", text)
            assert response.status_code == 404, response.text
            assert response.json() == {"detail": {"code": "project_not_found", "message": "Project does not exist."}}
    assert counts(r.context) == before and calls == []


def test_owned_idempotency_replay_is_unchanged_and_keys_are_project_scoped(async_resources):
    a, r = async_resources, async_resources.r
    replay = submit(r.context, r.own, r.headers, "owner-key")
    assert replay.status_code == 202 and replay.json() == a.own
    conflict = submit(r.context, r.own, r.headers, "owner-key", "different task")
    assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "idempotency_conflict"
    other_project = submit(r.context, r.sibling, r.headers, "other-key")
    assert other_project.status_code == 202
    assert other_project.json()["run_id"] not in {a.own["run_id"], a.other["run_id"]}
    assert len(r.context.app.state.task_dispatcher.agent_run_task_ids) == 3


def test_service_owned_entry_points_fail_closed_without_http(async_resources, monkeypatch):
    a, r = async_resources, async_resources.r
    calls = forbidden_access(r.context, monkeypatch, submission=True)
    with r.context.uow_factory() as uow:
        tasks = BackgroundTaskService(uow, r.context.app.state.task_dispatcher)
        agents = build_agent_run_service(uow, background_tasks=tasks, projects=project_service(r.context, uow),
            adapter=r.context.app.state.adapter, agent_workspace=r.context.app.state.agent_workspace)
        for owner in [r.other_owner, None]:
            for operation, code in [
                (lambda: agents.submit(r.own, "private task", "owner-key", owner_id=owner), "project_not_found"),
                (lambda: agents.get_owned(UUID(a.own["run_id"]), owner), "agent_run_not_found"),
                (lambda: agents.get_trace(UUID(a.own["run_id"]), owner_id=owner), "agent_run_not_found"),
                (lambda: tasks.get_owned(UUID(a.own["task_id"]), owner), "background_task_not_found"),
            ]:
                with pytest.raises(BackendError) as failure:
                    operation()
                assert failure.value.code == code
    assert calls == []


def test_enqueue_failure_retains_owned_linked_records_and_replay(async_resources, monkeypatch):
    r = async_resources.r

    def unavailable(task_id):
        raise RuntimeError("private broker connection")

    monkeypatch.setattr(r.context.app.state.task_dispatcher, "enqueue_agent_run", unavailable)
    first = submit(r.context, r.sibling, r.headers, "broker-failure-key")
    assert first.status_code == 503 and "private broker connection" not in first.text
    retry = submit(r.context, r.sibling, r.headers, "broker-failure-key")
    assert retry.status_code == 202, retry.text
    for path in reads(retry.json())[:2]:
        own = r.context.client.get(path, headers=r.headers)
        assert own.status_code == 200 and own.json()["status"] == "queued"
        assert r.context.client.get(path, headers=r.other_headers).status_code == 404


@pytest.mark.parametrize("field", ["owner_id", "user_id", "snapshot_id"])
def test_submission_cannot_override_server_identity_or_snapshot(async_resources, field):
    a, r = async_resources, async_resources.r
    before = counts(r.context)
    response = r.context.client.post(ROOT, headers=r.headers,
        json={"project_id": str(r.own), "task": "private task", field: str(r.foreign_snapshot.id)})
    assert response.status_code == 422 and counts(r.context) == before


def test_authenticated_submission_executes_with_trusted_worker_and_preserves_trace(resources):
    r, context = resources, resources.context
    accepted = context.client.post(f"/api/v1/projects/{r.own}/index", headers=r.headers)
    assert accepted.status_code == 202
    execute_index(context, accepted.json()["task_id"])
    queued = submit(context, r.own, r.headers, "worker-key")
    assert queued.status_code == 202
    with context.uow_factory() as uow:
        tasks = BackgroundTaskService(uow)
        agents = build_agent_run_service(uow, background_tasks=tasks, projects=project_service(context, uow),
            adapter=context.app.state.adapter, agent_workspace=context.app.state.agent_workspace)
        completed = agents.execute(UUID(queued.json()["task_id"]))
    assert completed.status == "completed" and completed.result == "Offline HTTP answer"
    for path in reads(queued.json())[:2]:
        response = context.client.get(path, headers=r.headers)
        assert response.status_code == 200 and response.json()["status"] == "completed"
    trace = context.client.get(reads(queued.json())[2], headers=r.headers)
    assert trace.status_code == 200
    events = trace.json()["events"]
    assert events[0]["event_type"] == "run_started" and events[-1]["event_type"] == "run_completed"
    assert [event["sequence"] for event in events] == list(range(len(events)))


def test_openapi_exposes_bearer_for_async_http_but_leaves_sse_to_b06(auth_context):
    schema = auth_context.client.get("/openapi.json").json()
    for path, method in [("/api/v1/tasks/{task_id}", "get"), (ROOT, "post"),
                         (ROOT + "/{run_id}", "get"), (ROOT + "/{run_id}/trace", "get")]:
        operation = schema["paths"][path][method]
        assert operation["security"] == [{"BearerAuth": []}] and "401" in operation["responses"]
    assert "/api/v1/tasks/health" not in schema["paths"]
    assert not schema["paths"][ROOT + "/{run_id}/events"]["get"].get("security")
