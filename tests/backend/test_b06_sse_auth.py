import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import logging
import os
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from redis import Redis as SyncRedis
from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import RedisError
from sqlalchemy import update
from starlette.requests import Request

from backend.app.api import realtime_trace
from backend.app.core.application_trace import ApplicationEventType, ApplicationTraceEvent
from backend.app.core.errors import BackendError
from backend.app.core.models import UserStatus
from backend.app.db.models import UserORM
from backend.app.infrastructure.realtime_trace import event_json, stream_key
from backend.app.repositories import InMemoryUnitOfWork
from test_b01_users import uow_factory
from test_b02_auth import auth_context
from test_b02_security import JWT_SECRET, claims_for
from test_b03_project_ownership import assert_unauthenticated
from test_b04_project_derived_auth import resources
from test_postgresql import postgresql_engine


def url(run_id):
    return f"/api/v1/agent-runs/{run_id}/events"


def trace_for(run, *, failed=False):
    now = datetime.now(timezone.utc)
    terminal = (ApplicationEventType.RUN_FAILED, {
        "stage": "backend", "error": {"category": "backend_error", "message": "safe failure"},
        "trace_complete": True,
    }) if failed else (ApplicationEventType.RUN_COMPLETED, {
        "stop_reason": "final", "has_result": True, "trace_complete": True,
    })
    return [
        ApplicationTraceEvent(0, ApplicationEventType.RUN_STARTED, now, None,
                              {"run_type": "agent", "snapshot_id": str(run.snapshot_id)}),
        ApplicationTraceEvent(1, ApplicationEventType.PLAN_CREATED, now, None,
                              {"source": "rule", "fallback_used": False, "steps": []}),
        ApplicationTraceEvent(2, terminal[0], now, None, terminal[1]),
    ]


@pytest.fixture
def streams(resources):
    r = resources
    with r.context.uow_factory() as uow, uow.transaction():
        runs = [uow.agent_runs.create_queued(project, uow.snapshots.get_by_commit(project, "a" * 40).id,
                 task="private SSE task", run_type="agent", model=None)
                for project in [r.own, r.foreign, r.legacy]]
    return SimpleNamespace(r=r, own=runs[0], foreign=runs[1], legacy=runs[2])


def finish(context, run, events, *, failed=False):
    with context.uow_factory() as uow, uow.transaction():
        current = uow.agent_runs.get(run.id)
        if current.status == "queued":
            uow.agent_runs.mark_running(run.id)
        if failed:
            uow.agent_runs.fail(run.id, "safe failure")
        else:
            uow.agent_runs.complete(run.id, "private result")
        for event in events:
            uow.trace_events.create(run.id, event.sequence, event.event_type.value,
                                    tool_name=event.tool_name, payload=event.database_payload())


def entries(events, start=1):
    return [(f"{index}-0".encode(), {b"event": event_json(event).encode()})
            for index, event in enumerate(events, start)]


class StreamRedis:
    def __init__(self, replay=(), batches=(), on_read=None):
        self.replay, self.batches, self.on_read = list(replay), list(batches), on_read
        self.calls = []

    async def xrange(self, key):
        self.calls.append(("xrange", key))
        return entries(self.replay)

    async def xread(self, streams, **kwargs):
        self.calls.append(("xread", streams, kwargs))
        if self.on_read is not None:
            self.on_read()
        if not self.batches:
            raise AssertionError("Unexpected unbounded stream read")
        batch = self.batches.pop(0)
        if isinstance(batch, BaseException):
            raise batch
        return [(next(iter(streams)).encode(), batch)] if batch else []

    async def aclose(self):
        pass


def frames(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


def guard_private_reads(context, monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append("private read")
        raise AssertionError("Authorization must precede Trace reads and Redis access")

    with context.uow_factory() as uow:
        monkeypatch.setattr(type(uow.agent_runs), "get", forbidden)
        monkeypatch.setattr(type(uow.trace_events), "list_for_run", forbidden)
    monkeypatch.setattr(realtime_trace.Redis, "from_url", forbidden)
    context.app.state.realtime_redis = StreamRedis()
    monkeypatch.setattr(context.app.state.realtime_redis, "xrange", forbidden)
    monkeypatch.setattr(context.app.state.realtime_redis, "xread", forbidden)
    return calls


@pytest.mark.parametrize("mode", ["missing", "malformed", "signature", "expired", "disabled", "query_token"])
def test_sse_requires_verified_identity_before_private_reads(streams, monkeypatch, caplog, mode):
    s, r = streams, streams.r
    context, headers, params = r.context, {}, {}
    finish(context, s.own, trace_for(s.own))
    if mode == "malformed":
        headers = {"Authorization": "Bearer not-a-jwt"}
    elif mode in {"signature", "expired"}:
        claims = claims_for(r.owner)
        if mode == "expired":
            claims.update(iat=1, nbf=1, exp=2)
        token = jwt.encode(claims, "wrong-SSE-test-signing-key-" * 2 if mode == "signature" else JWT_SECRET,
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
    calls = guard_private_reads(context, monkeypatch)
    with caplog.at_level(logging.ERROR, logger="pycode.backend"):
        for last_id in [None, "0"]:
            response = context.client.get(url(s.own.id), headers={**headers,
                **({"Last-Event-ID": last_id} if last_id is not None else {})}, params=params)
            assert_unauthenticated(response)
            assert response.headers["content-type"].startswith("application/json")
    assert calls == []
    token = r.headers["Authorization"].removeprefix("Bearer ")
    assert token not in response.text + caplog.text
    # An absent client must not even initialize a Redis connection.
    context.app.state.realtime_redis = None
    assert_unauthenticated(context.client.get(url(s.own.id), headers=headers, params=params))
    assert context.app.state.realtime_redis is None and calls == []


@pytest.mark.parametrize("state", ["queued", "running", "completed", "failed"])
def test_foreign_legacy_and_unknown_streams_are_hidden_before_replay(streams, monkeypatch, state):
    s, r = streams, streams.r
    context = r.context
    for run in [s.own, s.foreign, s.legacy]:
        if state in {"completed", "failed"}:
            finish(context, run, trace_for(run, failed=state == "failed"), failed=state == "failed")
        elif state == "running":
            with context.uow_factory() as uow, uow.transaction():
                uow.agent_runs.mark_running(run.id)
    calls = guard_private_reads(context, monkeypatch)
    for identifier, headers in [(s.foreign.id, r.headers), (s.own.id, r.other_headers),
                                (s.legacy.id, r.headers), (uuid4(), r.headers)]:
        for last_id in [None, "0", "invalid"]:
            response = context.client.get(url(identifier), headers={**headers,
                **({"Last-Event-ID": last_id} if last_id is not None else {})},
                params={"owner_id": str(r.other_owner)})
            assert response.status_code == 404, response.text
            assert response.json() == {"detail": {"code": "agent_run_not_found", "message": "Agent run does not exist."}}
            assert response.headers["content-type"].startswith("application/json")
    assert calls == []
    context.app.state.realtime_redis = None
    assert context.client.get(url(s.foreign.id), headers=r.headers).status_code == 404
    assert context.app.state.realtime_redis is None and calls == []


@pytest.mark.parametrize("failed", [False, True])
def test_owner_terminal_replay_and_resume_preserve_exact_events(streams, failed, caplog):
    s, r = streams, streams.r
    own, foreign = trace_for(s.own, failed=failed), trace_for(s.foreign, failed=failed)
    finish(r.context, s.own, own, failed=failed)
    finish(r.context, s.foreign, foreign, failed=failed)
    redis = StreamRedis(own)
    r.context.app.state.realtime_redis = redis
    with caplog.at_level(logging.ERROR, logger="pycode.backend"):
        complete = r.context.client.get(url(s.own.id), headers=r.headers)
        resumed = r.context.client.get(url(s.own.id), headers={**r.headers, "Last-Event-ID": "0"})
        exhausted = r.context.client.get(url(s.own.id), headers={**r.headers, "Last-Event-ID": "2"})
    expected = [json.loads(event_json(event)) for event in own]
    assert complete.status_code == resumed.status_code == exhausted.status_code == 200
    assert frames(complete.text) == expected and frames(resumed.text) == expected[1:]
    assert frames(exhausted.text) == []
    assert "id: 0\nevent: trace" in complete.text and "id: 0\n" not in resumed.text
    assert complete.headers["content-type"].startswith("text/event-stream")
    assert complete.headers["cache-control"] == "no-cache" and complete.headers["x-accel-buffering"] == "no"
    assert redis.calls == [("xrange", stream_key(s.own.id))] * 2
    token = r.headers["Authorization"].removeprefix("Bearer ")
    assert token not in complete.text + resumed.text + caplog.text
    assert str(s.foreign.id) not in complete.text and str(s.foreign.snapshot_id) not in complete.text


def test_live_redis_reads_and_reconnect_keep_sequence_and_cursor(streams):
    s, r = streams, streams.r
    events = trace_for(s.own)
    redis = StreamRedis(events[:1], [entries(events[1:2], 2), entries(events[2:], 3)])
    r.context.app.state.realtime_redis = redis
    live = r.context.client.get(url(s.own.id), headers=r.headers)
    assert live.status_code == 200 and frames(live.text) == [json.loads(event_json(event)) for event in events]
    assert [call[1] for call in redis.calls[1:]] == [
        {stream_key(s.own.id): "1-0"}, {stream_key(s.own.id): "2-0"},
    ]
    redis = StreamRedis(events[:2], [entries(events[2:], 3)])
    r.context.app.state.realtime_redis = redis
    resumed = r.context.client.get(url(s.own.id), headers={**r.headers, "Last-Event-ID": "0"})
    assert resumed.status_code == 200
    assert [event["sequence"] for event in frames(resumed.text)] == [1, 2]
    assert redis.calls[-1][1] == {stream_key(s.own.id): "2-0"}


def test_live_poll_uses_owned_database_terminal_fallback(streams, monkeypatch):
    s, r = streams, streams.r
    events = trace_for(s.own)
    redis = StreamRedis(events[:1], [[]], on_read=lambda: finish(r.context, s.own, events))
    r.context.app.state.realtime_redis = redis

    def unscoped(*args, **kwargs):
        raise AssertionError("Every database snapshot must remain owner-scoped")

    # Persist the terminal state in xread's callback before forbidding unscoped reads.
    def terminal():
        finish(r.context, s.own, events)
        with r.context.uow_factory() as uow:
            monkeypatch.setattr(type(uow.agent_runs), "get", unscoped)

    redis.on_read = terminal
    result = r.context.client.get(url(s.own.id), headers=r.headers)
    assert result.status_code == 200 and frames(result.text) == [json.loads(event_json(event)) for event in events]
    assert "realtime_trace_gap" not in result.text


@pytest.mark.parametrize("last_id", ["-1", "invalid", "", "1" * 20])
def test_owned_invalid_last_event_id_is_rejected_before_redis(streams, last_id):
    s, r = streams, streams.r
    redis = StreamRedis()
    r.context.app.state.realtime_redis = redis
    response = r.context.client.get(url(s.own.id), headers={**r.headers, "Last-Event-ID": last_id})
    assert response.status_code == 422 and response.json()["detail"]["code"] == "invalid_last_event_id"
    assert redis.calls == []


def test_gap_and_redis_outage_keep_existing_fallback_semantics(streams):
    s, r = streams, streams.r
    events = trace_for(s.own)
    redis = StreamRedis(events[1:])
    r.context.app.state.realtime_redis = redis
    gap = r.context.client.get(url(s.own.id), headers=r.headers)
    assert gap.status_code == 409 and gap.json()["detail"]["code"] == "realtime_trace_gap"
    r.context.app.state.realtime_redis = StreamRedis(events[:1] + events[2:])
    gap_after_headers = r.context.client.get(url(s.own.id), headers=r.headers)
    assert gap_after_headers.status_code == 200 and "event: realtime_trace_gap\n" in gap_after_headers.text

    class UnavailableRedis(StreamRedis):
        async def xrange(self, key):
            self.calls.append(("xrange", key))
            raise ConnectionError("private Redis connection details")

    unavailable = UnavailableRedis()
    r.context.app.state.realtime_redis = unavailable
    response = r.context.client.get(url(s.own.id), headers=r.headers)
    assert response.status_code == 503 and "private Redis" not in response.text
    finish(r.context, s.own, events)
    replay = r.context.client.get(url(s.own.id), headers=r.headers)
    assert replay.status_code == 200 and len(frames(replay.text)) == 3
    resumed = r.context.client.get(url(s.own.id), headers={**r.headers, "Last-Event-ID": "0"})
    assert resumed.status_code == 503 and resumed.json()["detail"]["code"] == "realtime_trace_unavailable"


@pytest.mark.parametrize("cancel", [False, True])
def test_disconnect_or_cancellation_stops_live_reads(streams, cancel):
    s, r = streams, streams.r
    events = trace_for(s.own)
    redis = StreamRedis(events[:1], [asyncio.CancelledError()])
    r.context.app.state.realtime_redis = redis

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def consume():
        request = Request({"type": "http", "app": r.context.app, "headers": []}, receive)
        response = await realtime_trace.stream_agent_run_events(request, s.own.id, owner_id=r.owner)
        iterator = response.body_iterator
        first = await anext(iterator)
        assert "id: 0\nevent: trace" in first
        assert len(redis.calls) == 1  # First frame arrives before any blocking xread.
        if cancel:
            with pytest.raises(StopAsyncIteration):
                await anext(iterator)
        else:
            await iterator.aclose()
        assert len(redis.calls) == (2 if cancel else 1)

    asyncio.run(consume())


def test_real_redis_stream_isolation_and_resume(streams, monkeypatch):
    s, r = streams, streams.r
    redis_url = os.environ.get("TEST_REDIS_URL") or os.environ.get("REDIS_URL")
    if not redis_url:
        pytest.skip("TEST_REDIS_URL or REDIS_URL is required for Redis integration")
    client = SyncRedis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
    try:
        client.ping()
    except (RedisError, OSError) as exc:
        client.close()
        pytest.skip(f"Redis integration unavailable: {type(exc).__name__}")
    own_key, foreign_key = stream_key(s.own.id), stream_key(s.foreign.id)
    assert not client.exists(own_key, foreign_key)
    try:
        own_events = trace_for(s.own)
        for key, run in [(own_key, s.own), (foreign_key, s.foreign)]:
            initial = own_events[:1] if key == own_key else trace_for(run)
            for event in initial:
                client.xadd(key, {"event": event_json(event)})
            client.expire(key, 60)
        redis = AsyncRedis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        r.context.app.state.realtime_redis = redis
        original = redis.xrange
        original_read = redis.xread
        reads = []
        live_reads = []

        async def xrange(key, *args, **kwargs):
            reads.append(key)
            return await original(key, *args, **kwargs)

        async def xread(streams, **kwargs):
            live_reads.append(streams)
            assert set(streams) == {own_key}
            for event in own_events[1:]:
                client.xadd(own_key, {"event": event_json(event)})
            return await original_read(streams, **kwargs)

        monkeypatch.setattr(redis, "xrange", xrange)
        monkeypatch.setattr(redis, "xread", xread)
        response = r.context.client.get(url(s.own.id), headers=r.headers)
        resumed = r.context.client.get(url(s.own.id), headers={**r.headers, "Last-Event-ID": "0"})
        assert response.status_code == resumed.status_code == 200
        assert [event["sequence"] for event in frames(response.text)] == [0, 1, 2]
        assert [event["sequence"] for event in frames(resumed.text)] == [1, 2]
        assert r.context.client.get(url(s.foreign.id), headers=r.headers).status_code == 404
        assert reads == [own_key, own_key]
        assert len(live_reads) == 1
        assert str(s.foreign.snapshot_id) not in response.text
    finally:
        client.delete(own_key, foreign_key)
        client.close()
