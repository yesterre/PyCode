"""Connected release acceptance; real Core, SQL/Memory and Redis boundaries.

Only the remote Git source, paid model transport and Celery delivery are offline
fixtures. The production Celery task adapters execute durable IDs without JWTs.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
import json
import os
from threading import Event
from types import SimpleNamespace
from uuid import UUID

import pytest
from redis.asyncio import Redis as AsyncRedis

from backend.app.config import AdmissionSettings
from backend.app.infrastructure.projects import ProjectOperationLocks, RedisProjectLockBackend
from backend.app.infrastructure.realtime_trace import RedisStreamPublisher, stream_key
from backend.app.runtime import build_agent_run_service, build_project_service
from backend.app.services.admission import AdmissionControl
from backend.app.infrastructure.redis import RedisRateLimiter, RedisSettings
from backend.app.services.background_tasks import BackgroundTaskService
from backend.app.tasks import agent_runs, indexing
from pycode.storage import load_index
from test_b01_users import uow_factory
from test_b02_auth import auth_context
from test_b03_project_ownership import actor
from test_b05_async_auth import counts
from test_b11_admission import rate_redis
from test_postgresql import postgresql_engine


REPO = "https://github.com/example/demo.git"


def body(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def error(response, status, code):
    assert body(response, status)["detail"]["code"] == code


def frames(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "realtime_trace_gap" not in response.text
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


@pytest.fixture
def journey(auth_context, rate_redis, monkeypatch):
    context, state = auth_context, auth_context.app.state
    redis, rate_calls, _ = rate_redis
    redis_url = os.environ.get("TEST_REDIS_URL") or os.environ["REDIS_URL"]
    state.admission = AdmissionControl(
        AdmissionSettings(index_requests=2, agent_requests=1, analysis_requests=2,
                          active_tasks=1, window_seconds=3600), RedisRateLimiter(redis),
    )
    state.project_operations = ProjectOperationLocks(RedisProjectLockBackend(client=redis))
    state.realtime_redis = AsyncRedis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=8)
    publisher = RedisStreamPublisher(RedisSettings(redis_url))
    publisher._client = redis  # Reuse the fixture-owned real connection and cleanup.
    stream_keys = set()

    def projects(uow):
        return build_project_service(uow, adapter=state.adapter, workspace=state.workspace,
                                     artifacts=state.artifacts, operations=state.project_operations)

    @contextmanager
    def index_services():
        with context.uow_factory() as uow:
            yield BackgroundTaskService(uow), projects(uow)

    @contextmanager
    def agent_service():
        with context.uow_factory() as uow:
            yield build_agent_run_service(
                uow, background_tasks=BackgroundTaskService(uow), projects=projects(uow),
                adapter=state.adapter, agent_workspace=state.agent_workspace, trace_publisher=publisher,
            )

    monkeypatch.setattr(indexing, "worker_repository_index_services", index_services)
    monkeypatch.setattr(agent_runs, "worker_agent_run_service", agent_service)
    try:
        yield SimpleNamespace(context=context, state=state, redis=redis, rate_calls=rate_calls,
                              streams=stream_keys, index=indexing.run_repository_index_task.run,
                              agent=agent_runs.run_agent_task.run)
    finally:
        if stream_keys:
            redis.delete(*stream_keys)


def index(j, user, key="index"):
    return j.context.client.post(user.base + "/index", headers={**user.headers, "Idempotency-Key": key})


def agent(j, user, key="agent", task=None):
    return j.context.client.post("/api/v1/agent-runs", headers={**user.headers, "Idempotency-Key": key},
        json={"project_id": user.project, "task": task or user.question})


def private_routes(user):
    return [user.base, user.base + "/snapshots", user.base + f"/snapshots/{user.snapshot}",
            user.base + f"/snapshots/{user.snapshot}/graph?q=main", user.base + "/analysis-history",
            f"/api/v1/tasks/{user.index_task}", f"/api/v1/tasks/{user.agent_task}",
            f"/api/v1/agent-runs/{user.run}", f"/api/v1/agent-runs/{user.run}/trace",
            f"/api/v1/agent-runs/{user.run}/events"]


def hidden(j, owner, outsider, llm):
    """All denial checks use the IDs produced by this business journey."""
    client, state = j.context.client, j.state
    before = (len(llm.prompts), list(state.task_dispatcher.repository_index_task_ids),
              list(state.task_dispatcher.agent_run_task_ids), len(j.rate_calls), counts(j.context))
    for url in private_routes(owner):
        assert client.get(url, headers=outsider.headers).status_code == 404, url
        assert client.get(url).status_code == 401, url
    for suffix, data in [("archive", None), ("restore", None), ("index", None),
                         ("ask", {"question": "entry"}), ("impact", {"file_path": "main.py"})]:
        assert client.post(owner.base + "/" + suffix, headers=outsider.headers, json=data).status_code == 404
        assert client.post(owner.base + "/" + suffix, json=data).status_code == 401
    assert agent(j, SimpleNamespace(**{**vars(owner), "headers": outsider.headers})).status_code == 404
    assert client.post("/api/v1/agent-runs", json={"project_id": owner.project, "task": "entry"}).status_code == 401
    # Supplying the victim's Snapshot under an owned Project is also forbidden.
    for suffix in ["", "/graph?q=main"]:
        assert client.get(outsider.base + f"/snapshots/{owner.snapshot}" + suffix,
                          headers=outsider.headers).status_code == 404
    assert before == (len(llm.prompts), list(state.task_dispatcher.repository_index_task_ids),
                      list(state.task_dispatcher.agent_run_task_ids), len(j.rate_calls), counts(j.context))


def live_worker(j, user, monkeypatch):
    """Subscribe before executing the real task, forcing a real Redis XREAD."""
    start, reads = Event(), []
    redis = j.state.realtime_redis
    original = redis.xread

    async def read(streams, **kwargs):
        reads.append(streams)
        assert set(streams) == {stream_key(UUID(user.run))}
        start.set()
        return await original(streams, **kwargs)

    def execute():
        assert start.wait(30), "SSE never reached live Redis reading"
        j.agent(user.agent_task)

    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=1) as pool:
        patch.setattr(redis, "xread", read)
        worker = pool.submit(execute)
        try:
            response = j.context.client.get(f"/api/v1/agent-runs/{user.run}/events", headers=user.headers)
        finally:
            start.set()
        worker.result(timeout=30)
    events = frames(response)
    assert reads and events
    assert [event["sequence"] for event in events] == list(range(len(events)))
    assert events[0]["event_type"] == "run_started"
    assert events[-1]["event_type"] == "run_completed"
    return response, events


def test_two_users_complete_release_journey_with_live_trace_and_lifecycle(journey, llm, monkeypatch):
    j, users, client = journey, [], journey.context.client
    assert body(client.get("/openapi.json"))["info"]["version"] == "3.0.0"
    # A trusted legacy record remains inaccessible even after both registrations.
    with j.context.uow_factory() as uow, uow.transaction():
        legacy = uow.projects.create("unclaimed legacy", REPO)
    for account in ["alice", "bob"]:
        identifier, headers = actor(j.context, account)
        assert body(client.get("/api/v1/auth/me", headers=headers))["id"] == str(identifier)
        error(client.post("/api/v1/auth/login", json={"account_name": account, "password": "incorrect"}),
              401, "unauthenticated")
        assert client.post("/api/v1/projects", headers=headers,
                           json={"name": "invalid", "repo_url": REPO, "owner_id": str(identifier)}).status_code == 422
        project = body(client.post("/api/v1/projects", headers=headers,
                                  json={"name": account, "repo_url": REPO}), 201)
        user = SimpleNamespace(id=identifier, headers=headers, project=project["id"],
            base="/api/v1/projects/" + project["id"], question=f"Explain main entry for {account}")
        users.append(user)
        assert client.get(f"/api/v1/projects/{legacy.id}", headers=headers).status_code == 404
        error(client.post(user.base + "/ask", headers=headers, json={"question": "entry"}), 409, "project_not_ready")
        assert client.post(user.base + "/ask", headers=headers, json={"question": " "}).status_code == 422
        user.accepted_index = body(index(j, user), 202)
        user.index_task = user.accepted_index["task_id"]
        assert body(index(j, user), 202) == user.accepted_index
        error(agent(j, user), 409, "project_not_ready")
        j.index(user.index_task)
        j.index(user.index_task)  # Redelivery must not rebuild or claim twice.
        task = body(client.get(f"/api/v1/tasks/{user.index_task}", headers=headers))
        assert task["status"] == "completed" and task["attempt_count"] == 1
        user.snapshot = task["snapshot_id"]
        snapshot = body(client.get(user.base + f"/snapshots/{user.snapshot}", headers=headers))
        assert snapshot["status"] == "ready" and snapshot["project_id"] == user.project
        assert snapshot["commit_sha"] == "a" * 40
        detail = body(client.get(user.base, headers=headers))
        assert detail["current_snapshot_id"] == user.snapshot and detail["index_summary"]["file_count"] == 2
        with j.context.uow_factory() as uow, uow.transaction():
            stored = uow.snapshots.get(UUID(user.snapshot))
        user.bundle = j.state.artifacts.read_ready(UUID(user.project), stored)
        user.evidence = (user.bundle.index_path.read_bytes(), user.bundle.graph_path.read_bytes())
        assert {file.path for file in load_index(user.bundle.index_path).files} == {"main.py", "service.py"}
        assert body(client.get(user.base + f"/snapshots/{user.snapshot}/graph?q=main", headers=headers))["nodes"]
        for suffix, payload in [("ask", {"question": user.question}), ("impact", {"file_path": "service.py"})]:
            result = body(client.post(user.base + "/" + suffix, headers=headers, json=payload))
            assert result["project_id"] == user.project and result["evidence"]
            assert result["answer"] == "Offline HTTP answer"
        user.history = body(client.get(user.base + "/analysis-history", headers=headers))["items"]
        assert {item["mode"] for item in user.history} == {"repository", "impact"}
        error(client.post(user.base + "/ask", headers=headers, json={"question": "entry"}), 429, "rate_limited")
        user.accepted_agent = body(agent(j, user), 202)
        user.run, user.agent_task = user.accepted_agent["run_id"], user.accepted_agent["task_id"]
        j.streams.add(stream_key(UUID(user.run)))
        assert body(agent(j, user), 202) == user.accepted_agent
        error(agent(j, user, task="different"), 409, "idempotency_conflict")
        error(index(j, user, "over-quota"), 429, "task_quota_exceeded")

    a, b = users
    assert a.snapshot != b.snapshot and a.run != b.run and a.index_task != b.index_task
    assert a.bundle.index_path != b.bundle.index_path and a.bundle.graph_path != b.bundle.graph_path
    for owner, outsider in [(a, b), (b, a)]:
        hidden(j, owner, outsider, llm)
        assert {p["id"] for p in body(client.get("/api/v1/projects", headers=owner.headers))["projects"]} == {owner.project}
        archived = body(client.post(owner.base + "/archive", headers=owner.headers))
        assert archived["archived_at"] is not None
        repeated = body(client.post(owner.base + "/archive", headers=owner.headers))
        assert datetime.fromisoformat(repeated["archived_at"]) == datetime.fromisoformat(archived["archived_at"])
        assert body(client.get("/api/v1/projects", headers=owner.headers))["projects"] == []
        assert [p["id"] for p in body(client.get("/api/v1/projects?archive_status=archived", headers=owner.headers))["projects"]] == [owner.project]
        for suffix, payload in [("index", None), ("ask", {"question": "entry"}), ("impact", {"file_path": "main.py"})]:
            error(client.post(owner.base + "/" + suffix, headers=owner.headers, json=payload), 409, "project_archived")
        error(agent(j, owner, "archived-new"), 409, "project_archived")
        assert body(agent(j, owner), 202) == owner.accepted_agent
        assert body(index(j, owner), 202)["task_id"] == owner.index_task
        hidden(j, owner, outsider, llm)
        live, events = live_worker(j, owner, monkeypatch)
        j.agent(owner.agent_task)  # Terminal redelivery cannot re-enter Core.
        run = body(client.get(f"/api/v1/agent-runs/{owner.run}", headers=owner.headers))
        assert run["status"] == "completed" and run["snapshot_id"] == owner.snapshot and run["core_started_at"]
        task = body(client.get(f"/api/v1/tasks/{owner.agent_task}", headers=owner.headers))
        assert task["status"] == "completed" and task["attempt_count"] == 1 and task["agent_run_id"] == owner.run
        trace = body(client.get(f"/api/v1/agent-runs/{owner.run}/trace", headers=owner.headers))["events"]
        # Public JSON and SSE use normalized UTC timestamps; require exact events.
        assert events == trace
        replay = client.get(f"/api/v1/agent-runs/{owner.run}/events", headers=owner.headers)
        assert frames(replay) == events
        resume = client.get(f"/api/v1/agent-runs/{owner.run}/events", headers={**owner.headers, "Last-Event-ID": "0"})
        assert frames(resume) == events[1:]
        assert frames(client.get(f"/api/v1/agent-runs/{owner.run}/events", headers={
            **owner.headers, "Last-Event-ID": str(events[-1]["sequence"])})) == []
        assert client.get(f"/api/v1/agent-runs/{owner.run}/events", params={"access_token": owner.headers["Authorization"].split()[1]}).status_code == 401
        history = body(client.get(owner.base + "/analysis-history", headers=owner.headers))["items"]
        assert {row["id"] for row in history} == {owner.run, *(row["id"] for row in owner.history)}
        assert {row["mode"] for row in history} == {"repository", "impact", "agent"}
        assert run["result"] == next(row["answer"] for row in history if row["id"] == owner.run)
        assert body(client.get(owner.base + "/analysis-history?limit=1&offset=1", headers=owner.headers))["items"] == history[1:2]
        assert body(client.post(owner.base + "/restore", headers=owner.headers))["archived_at"] is None
        assert body(client.post(owner.base + "/restore", headers=owner.headers))["archived_at"] is None
        hidden(j, owner, outsider, llm)
        reindex = body(index(j, owner, "restored-index"), 202)
        j.index(reindex["task_id"])
        assert body(client.get(owner.base, headers=owner.headers))["current_snapshot_id"] == owner.snapshot
        assert [row["id"] for row in body(client.get(owner.base + "/snapshots", headers=owner.headers))["snapshots"]] == [owner.snapshot]
        assert owner.evidence == (owner.bundle.index_path.read_bytes(), owner.bundle.graph_path.read_bytes())
        error(index(j, owner, "over-rate"), 429, "rate_limited")
        error(agent(j, owner, "over-rate"), 429, "rate_limited")
        for url in private_routes(owner):
            assert client.get(url, headers=owner.headers).status_code == 200, url
        token = owner.headers["Authorization"].split()[1]
        assert token not in live.text + replay.text + resume.text + str(trace)
        assert outsider.snapshot not in live.text and outsider.run not in live.text
    assert len(j.state.task_dispatcher.repository_index_task_ids) == 4
    assert len(j.state.task_dispatcher.agent_run_task_ids) == 2
    for user in users:
        assert j.redis.get(f"pycode:rate:v1:{user.id}:index") == b"2"
        assert j.redis.get(f"pycode:rate:v1:{user.id}:agent") == b"1"
        assert j.redis.get(f"pycode:rate:v1:{user.id}:analysis") == b"2"
    with j.context.uow_factory() as uow, uow.transaction():
        assert uow.projects.get(legacy.id).owner_id is None
    assert counts(j.context)[0] == 6
