import os
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import TimeoutError
from redis.retry import Retry

from backend.app.config import AdmissionSettings
from backend.app.core.errors import BackendError
from backend.app.core.models import IndexSummary, UserStatus
from backend.app.db.models import BackgroundTaskORM, UserORM
from backend.app.infrastructure.redis import RedisRateLimiter
from backend.app.services.admission import AdmissionControl
from test_b01_users import uow_factory
from test_b02_auth import auth_context
from test_b03_project_ownership import create
from test_b04_project_derived_auth import resources
from test_b05_async_auth import counts, submit
from test_b07_worker_isolation import mutate, ready, workers
from test_postgresql import postgresql_engine


@pytest.fixture
def rate_redis(monkeypatch):
    url = os.environ.get("TEST_REDIS_URL") or os.environ.get("REDIS_URL")
    if not url:
        pytest.skip("TEST_REDIS_URL or REDIS_URL is required for real admission tests")
    client = Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1,
                            retry=Retry(NoBackoff(), 0), max_connections=32)
    client.ping()
    keys, calls = set(), []
    original = client.eval

    def evaluate(script, count, key, *args):
        keys.add(key)
        calls.append(key)
        return original(script, count, key, *args)

    monkeypatch.setattr(client, "eval", evaluate)
    try:
        yield client, calls, keys
    finally:
        if keys:
            client.delete(*keys)
        client.close()


@pytest.fixture
def limited(resources, rate_redis, application):
    r = resources
    r.redis, r.calls, r.keys = rate_redis
    r.context.app.state.project_operations = application.state.project_operations
    r.configure = lambda **values: configure(r, **values)
    r.configure()
    return r


def configure(r, **values):
    settings = replace(AdmissionSettings(index_requests=1, agent_requests=1,
                                         analysis_requests=1, active_tasks=2), **values)
    r.policy = AdmissionControl(settings, RedisRateLimiter(r.redis))
    r.context.app.state.admission = r.policy


def index(r, project=None, *, key=None, headers=None):
    return r.context.client.post(f"/api/v1/projects/{project or r.own}/index",
        headers={**(r.headers if headers is None else headers),
                 **({"Idempotency-Key": key} if key is not None else {})})


def metadata(r, projects=None):
    with r.context.uow_factory() as uow, uow.transaction():
        for project in projects or [r.own, r.sibling, r.foreign]:
            snapshot = uow.snapshots.upsert(project, "a" * 40, index_artifact_path="unused/index.json",
                                           graph_artifact_path="unused/graph.json", summary=IndexSummary(1, 1, 0))
            uow.projects.set_current_snapshot(project, snapshot.id)


def terminal(r, identifier, *, failed):
    with r.context.uow_factory() as uow, uow.transaction():
        task = uow.background_tasks.claim(UUID(str(identifier))).task
        if task.agent_run_id is not None:
            uow.agent_runs.mark_running(task.agent_run_id)
            if failed:
                uow.agent_runs.fail(task.agent_run_id, "safe failure")
            else:
                uow.agent_runs.complete(task.agent_run_id, "answer")
        if failed:
            uow.background_tasks.mark_failed(task.id, execution_token=task.execution_token,
                                            error_code="model_failed", error_message="safe failure")
        else:
            uow.background_tasks.mark_completed(task.id, execution_token=task.execution_token)


def error(response, code, status=429):
    assert response.status_code == status, response.text
    assert response.json()["detail"]["code"] == code


def test_index_rate_replay_user_isolation_and_read_routes(limited):
    r = limited
    first = index(r, key="index-key")
    assert first.status_code == 202, first.text
    call_count, before = len(r.calls), counts(r.context)
    assert index(r, key="index-key").json() == first.json()
    assert len(r.calls) == call_count
    error(index(r, key="different-key"), "repository_index_in_progress", 409)
    error(index(r, r.sibling), "rate_limited")
    assert counts(r.context) == before
    assert r.redis.get(f"pycode:rate:v1:{r.owner}:index") == b"1"
    assert index(r, r.foreign, headers=r.other_headers).status_code == 202
    assert f"pycode:rate:v1:{r.owner}:index" in r.keys
    assert f"pycode:rate:v1:{r.other_owner}:index" in r.keys
    before_reads = len(r.calls)
    for url in ["/api/v1/projects", f"/api/v1/projects/{r.own}",
                f"/api/v1/tasks/{first.json()['task_id']}", f"/api/v1/projects/{r.own}/analysis-history"]:
        assert r.context.client.get(url, headers=r.headers).status_code == 200
    assert len(r.calls) == before_reads
    assert len(r.context.app.state.task_dispatcher.repository_index_task_ids) == 2


def test_agent_rate_replay_conflict_and_no_partial_run(limited):
    r = limited
    metadata(r)
    first = submit(r.context, r.own, r.headers, "agent-key")
    assert first.status_code == 202, first.text
    call_count, before = len(r.calls), counts(r.context)
    assert submit(r.context, r.own, r.headers, "agent-key").json() == first.json()
    assert len(r.calls) == call_count
    error(submit(r.context, r.own, r.headers, "agent-key", task="changed"), "idempotency_conflict", 409)
    error(submit(r.context, r.sibling, r.headers, "second-key"), "rate_limited")
    assert counts(r.context) == before
    assert submit(r.context, r.foreign, r.other_headers).status_code == 202
    assert len(r.context.app.state.task_dispatcher.agent_run_task_ids) == 2


@pytest.mark.parametrize("failed", [False, True])
def test_task_quota_spans_operations_archive_and_terminal_release(limited, failed):
    r = limited
    r.configure(index_requests=100, agent_requests=100)
    metadata(r)
    first = index(r, key="index")
    second = submit(r.context, r.sibling, r.headers, "agent")
    assert first.status_code == second.status_code == 202
    assert r.context.client.post(f"/api/v1/projects/{r.own}/archive", headers=r.headers).status_code == 200
    before, calls = counts(r.context), len(r.calls)
    error(index(r, r.sibling), "task_quota_exceeded")
    error(submit(r.context, r.sibling, r.headers, "another-agent"), "task_quota_exceeded")
    assert counts(r.context) == before and len(r.calls) == calls
    assert index(r, key="index").json() == first.json()
    assert submit(r.context, r.sibling, r.headers, "agent").json() == second.json()
    assert index(r, r.foreign, headers=r.other_headers).status_code == 202
    terminal(r, first.json()["task_id"], failed=failed)
    third = index(r, r.sibling)
    assert third.status_code == 202, third.text
    terminal(r, second.json()["task_id"], failed=failed)
    assert submit(r.context, r.sibling, r.headers, "after-terminal").status_code == 202


def test_expired_running_lease_and_requeue_still_occupy_quota(limited):
    r = limited
    r.configure(active_tasks=1, index_requests=100)
    response = index(r)
    assert response.status_code == 202
    identifier = UUID(response.json()["task_id"])
    with r.context.uow_factory() as uow, uow.transaction():
        task = uow.background_tasks.claim(identifier).task
        mutate(uow, "background_tasks", BackgroundTaskORM, identifier,
               lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=60))
    error(index(r, r.sibling), "task_quota_exceeded")
    with r.context.uow_factory() as uow, uow.transaction():
        assert uow.background_tasks.requeue_if_stale(identifier, task.execution_token) is not None
    error(index(r, r.sibling), "task_quota_exceeded")
    terminal(r, identifier, failed=True)
    assert index(r, r.sibling).status_code == 202


def test_parallel_mixed_submissions_never_exceed_owner_quota(limited):
    r = limited
    r.configure(index_requests=100, agent_requests=100, active_tasks=3)
    projects = [r.own, r.sibling] + [UUID(create(r.context, r.headers)["id"]) for _ in range(6)]
    metadata(r, projects)
    barrier = Barrier(len(projects))

    def request(item):
        number, project = item
        barrier.wait(timeout=30)
        return (index(r, project) if number % 2 else submit(r.context, project, r.headers))

    before = counts(r.context)
    with ThreadPoolExecutor(max_workers=len(projects)) as pool:
        responses = list(pool.map(request, enumerate(projects)))
    assert sorted(response.status_code for response in responses) == [202] * 3 + [429] * 5, [x.text for x in responses]
    for response in responses:
        if response.status_code == 429:
            error(response, "task_quota_exceeded")
    with r.context.uow_factory() as uow, uow.transaction():
        assert uow.background_tasks.lock_owner_and_count_active(r.owner) == 3
    after = counts(r.context)
    assert after[0] - before[0] == 3
    assert len(r.context.app.state.task_dispatcher.repository_index_task_ids
               + r.context.app.state.task_dispatcher.agent_run_task_ids) == 3


def test_analysis_budget_shared_by_ask_and_impact_without_extra_run(limited, workers, llm):
    r = limited
    ready(r, workers)
    first = r.context.client.post(f"/api/v1/projects/{r.own}/ask", headers=r.headers,
                                  json={"question": "entry"})
    assert first.status_code == 200, first.text
    before, prompts = counts(r.context), len(llm.prompts)
    for path, payload in [("ask", {"question": "again"}), ("impact", {"file_path": "main.py"})]:
        error(r.context.client.post(f"/api/v1/projects/{r.own}/{path}", headers=r.headers, json=payload), "rate_limited")
    assert counts(r.context) == before and len(llm.prompts) == prompts
    assert r.redis.get(f"pycode:rate:v1:{r.owner}:analysis") == b"1"


def test_denied_and_invalid_requests_do_not_touch_rate_state(limited):
    r = limited
    before = counts(r.context)
    for project in [r.foreign, r.legacy, uuid4()]:
        error(index(r, project), "project_not_found", 404)
        error(submit(r.context, project, r.headers), "project_not_found", 404)
    assert r.context.client.post(f"/api/v1/projects/{r.own}/index").status_code == 401
    assert index(r, key="\t").status_code == 422
    assert submit(r.context, r.own, r.headers, task=" ").status_code == 422
    error(submit(r.context, r.own, r.headers), "project_not_ready", 409)
    assert r.context.client.post(f"/api/v1/projects/{r.own}/archive", headers=r.headers).status_code == 200
    error(index(r), "project_archived", 409)
    with r.context.uow_factory() as uow, uow.transaction():
        mutate(uow, "users", UserORM, r.owner, status=UserStatus.DISABLED)
    error(index(r), "unauthenticated", 401)
    assert counts(r.context) == before and not r.calls


def test_redis_outage_rejects_new_work_but_replays_remain_available(limited, monkeypatch, caplog):
    r = limited
    metadata(r)
    a, b = index(r, key="a"), submit(r.context, r.own, r.headers, "b")
    assert a.status_code == b.status_code == 202

    def unavailable(*args):
        raise TimeoutError("do-not-log-private-redis-detail")

    monkeypatch.setattr(r.redis, "eval", unavailable)
    r.configure(active_tasks=10)
    before = counts(r.context)
    error(index(r, r.sibling), "admission_unavailable", 503)
    error(submit(r.context, r.sibling, r.headers), "admission_unavailable", 503)
    assert counts(r.context) == before
    assert index(r, key="a").json() == a.json()
    assert submit(r.context, r.own, r.headers, "b").json() == b.json()
    assert "do-not-log-private-redis-detail" not in caplog.text


def test_real_connection_refusal_before_analysis_and_task_creation(limited, workers, llm):
    r = limited
    ready(r, workers)
    before, prompts = counts(r.context), len(llm.prompts)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        broken = Redis(host="127.0.0.1", port=reserved.getsockname()[1], socket_connect_timeout=0.1,
                       socket_timeout=0.1, retry=Retry(NoBackoff(), 0))
        r.policy.limiter = RedisRateLimiter(broken)
        try:
            error(index(r, r.sibling), "admission_unavailable", 503)
            error(submit(r.context, r.own, r.headers), "admission_unavailable", 503)
            error(r.context.client.post(f"/api/v1/projects/{r.own}/ask", headers=r.headers,
                                       json={"question": "entry"}), "admission_unavailable", 503)
        finally:
            broken.close()
    assert counts(r.context) == before and len(llm.prompts) == prompts


def test_worker_can_complete_accepted_work_after_archive_and_rate_outage(limited, workers, monkeypatch):
    r = limited
    response = index(r)
    assert response.status_code == 202
    assert r.context.client.post(f"/api/v1/projects/{r.own}/archive", headers=r.headers).status_code == 200

    def forbidden(*args):
        pytest.fail("Worker execution must not request user admission")

    monkeypatch.setattr(r.policy.limiter, "check", forbidden)
    workers.index(response.json()["task_id"])
    with r.context.uow_factory() as uow, uow.transaction():
        assert uow.background_tasks.lock_owner_and_count_active(r.owner) == 0


def test_agent_worker_preserves_trace_and_releases_quota_without_admission(limited, workers, monkeypatch):
    r = limited
    ready(r, workers)
    response = submit(r.context, r.own, r.headers, "accepted-agent")
    assert response.status_code == 202, response.text
    assert r.context.client.post(f"/api/v1/projects/{r.own}/archive", headers=r.headers).status_code == 200

    def forbidden(*args):
        pytest.fail("Accepted Agent execution must not request user admission")

    monkeypatch.setattr(r.policy.limiter, "check", forbidden)
    workers.agent(response.json()["task_id"])
    with r.context.uow_factory() as uow, uow.transaction():
        assert uow.background_tasks.lock_owner_and_count_active(r.owner) == 0
    run_url = f"/api/v1/agent-runs/{response.json()['run_id']}"
    assert r.context.client.get(run_url, headers=r.headers).json()["status"] == "completed"
    trace = r.context.client.get(run_url + "/trace", headers=r.headers)
    assert trace.status_code == 200 and trace.json()["events"]


def test_publish_failure_keeps_durable_quota_and_idempotency(limited, monkeypatch):
    r = limited
    r.configure(active_tasks=1)

    def unavailable(*args):
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(r.context.app.state.task_dispatcher, "enqueue_repository_index", unavailable)
    before = counts(r.context)
    error(index(r, key="publish"), "task_queue_unavailable", 503)
    after = counts(r.context)
    assert after[0] == before[0] + 1
    calls = len(r.calls)
    replay = index(r, key="publish")
    assert replay.status_code == 202, replay.text
    assert index(r, key="publish").json() == replay.json()
    error(index(r, r.sibling), "task_quota_exceeded")
    assert len(r.calls) == calls and counts(r.context) == after


def test_database_failure_rolls_back_task_but_conservatively_keeps_rate_charge(limited, monkeypatch):
    r = limited
    metadata(r)
    with r.context.uow_factory() as uow:
        repository_class = type(uow.agent_runs)

    def fail_after_admission(*args, **kwargs):
        raise BackendError("database_failed", "A database transaction failed.")

    before = counts(r.context)
    with monkeypatch.context() as patch:
        patch.setattr(repository_class, "create_queued", fail_after_admission)
        error(submit(r.context, r.own, r.headers, "database-failure"), "database_failed", 500)
    assert counts(r.context) == before
    assert r.redis.get(f"pycode:rate:v1:{r.owner}:agent") == b"1"
    error(submit(r.context, r.own, r.headers, "database-failure"), "rate_limited")
    assert counts(r.context) == before


def test_real_redis_lua_is_atomic_across_independent_limiters_and_expires(rate_redis):
    client, _, _ = rate_redis
    owner = uuid4()
    barrier = Barrier(20)

    def admit(_):
        barrier.wait(timeout=30)
        try:
            RedisRateLimiter(client).check(owner, "index", 5, 60)
            return "accepted"
        except BackendError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(admit, range(20)))
    assert results.count("accepted") == 5 and results.count("rate_limited") == 15
    key = f"pycode:rate:v1:{owner}:index"
    assert client.get(key) == b"5" and 0 < client.ttl(key) <= 60
    client.pexpire(key, 1)
    import time
    time.sleep(0.02)
    RedisRateLimiter(client).check(owner, "index", 5, 60)
    assert client.get(key) == b"1" and 0 < client.ttl(key) <= 60


@pytest.mark.parametrize("field", list(AdmissionSettings.__dataclass_fields__))
@pytest.mark.parametrize("invalid", [0, -1, True, 1.5])
def test_admission_settings_reject_invalid_limits(field, invalid):
    with pytest.raises(ValueError):
        AdmissionSettings(**{field: invalid})


def test_environment_settings_and_openapi_only_expensive_routes(application, monkeypatch):
    values = {"PYCODE_RATE_WINDOW_SECONDS": "7", "PYCODE_RATE_INDEX_REQUESTS": "2",
              "PYCODE_RATE_AGENT_REQUESTS": "3", "PYCODE_RATE_ANALYSIS_REQUESTS": "4",
              "PYCODE_MAX_ACTIVE_TASKS_PER_USER": "5"}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    assert AdmissionSettings.from_environment() == AdmissionSettings(7, 2, 3, 4, 5)
    monkeypatch.setenv("PYCODE_RATE_INDEX_REQUESTS", "bad")
    with pytest.raises(ValueError):
        AdmissionSettings.from_environment()
    paths = application.openapi()["paths"]
    for path in ["/api/v1/projects/{project_id}/index", "/api/v1/agent-runs",
                 "/api/v1/projects/{project_id}/ask", "/api/v1/projects/{project_id}/impact"]:
        assert "429" in paths[path]["post"]["responses"]
    assert "429" not in paths["/api/v1/projects"]["get"]["responses"]


def test_admission_io_bounds_override_redis_url_query_options(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/15?socket_timeout=30&socket_connect_timeout=30"
                       "&retry_on_timeout=true&max_connections=999")
    monkeypatch.setattr(Redis, "eval", lambda *args: 1)
    limiter = RedisRateLimiter()
    try:
        limiter.check(uuid4(), "index", 1, 60)
        options = limiter.client.connection_pool.connection_kwargs
        assert 0 < options["socket_timeout"] <= 1
        assert 0 < options["socket_connect_timeout"] <= 1
        assert not options["retry_on_timeout"] and options["retry"]._retries == 0
        assert limiter.client.connection_pool.max_connections == 32
    finally:
        limiter.close()
