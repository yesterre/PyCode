from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

from backend.app.core.application_trace import (
    ApplicationEventType, ApplicationTraceEvent, application_event_from_record,
)
from backend.app.core.realtime_trace import RealtimeTraceSession
from backend.app.infrastructure.realtime_trace import RedisStreamPublisher, event_json, stream_key
from backend.app.infrastructure.redis import RedisSettings
from pycode.agent.trace import TraceEvent


class CapturePublisher:
    def __init__(self):
        self.events = []

    def publish(self, run_id, event):
        self.events.append(event)


class ReplayRedis:
    def __init__(self, events):
        self.events = events

    async def xrange(self, key):
        return [
            (f"{index + 1}-0".encode(), {b"event": event_json(event).encode()})
            for index, event in enumerate(self.events)
        ]

    async def aclose(self):
        pass


def _execute(client, ready_url, application, agent_worker):
    accepted = client.post("/api/v1/agent-runs", json={
        "project_id": ready_url.rsplit("/", 1)[-1],
        "task": "analyze the entry point",
    })
    assert accepted.status_code == 202, accepted.text
    run_id = UUID(accepted.json()["run_id"])
    publisher = CapturePublisher()
    service = agent_worker.service()
    service.trace_publisher = publisher
    service.execute(UUID(accepted.json()["task_id"]))
    stored = [
        application_event_from_record(record)
        for record in application.state.persistence.trace_events.values()
        if record.run_id == run_id
    ]
    return run_id, publisher.events, stored


def test_live_events_match_terminal_postgres_and_sse_replay(
    client, ready_url, application, agent_worker,
):
    run_id, published, stored = _execute(client, ready_url, application, agent_worker)
    assert len(published) == len(stored) > 2
    assert [event_json(event) for event in published] == [event_json(event) for event in stored]
    assert [event.sequence for event in stored] == list(range(len(stored)))
    assert stored[0].event_type == "run_started"
    assert stored[-1].event_type == "run_completed"
    assert stored[-1].payload["trace_complete"] is True

    application.state.realtime_redis = ReplayRedis(published)
    full = client.get(f"/api/v1/agent-runs/{run_id}/events")
    assert full.status_code == 200
    assert full.text.count("event: trace") == len(stored)
    assert f"id: {stored[-1].sequence}" in full.text

    resumed = client.get(
        f"/api/v1/agent-runs/{run_id}/events", headers={"Last-Event-ID": "0"},
    )
    assert resumed.status_code == 200
    assert resumed.text.count("event: trace") == len(stored) - 1
    assert "id: 0\n" not in resumed.text


def test_terminal_resume_rejects_missing_or_changed_redis_prefix(
    client, ready_url, application, agent_worker,
):
    run_id, published, _ = _execute(client, ready_url, application, agent_worker)
    application.state.realtime_redis = ReplayRedis(published[1:])
    response = client.get(
        f"/api/v1/agent-runs/{run_id}/events", headers={"Last-Event-ID": "1"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "realtime_trace_gap"

    application.state.realtime_redis = ReplayRedis(published)
    response = client.get(
        f"/api/v1/agent-runs/{run_id}/events", headers={"Last-Event-ID": "-1"},
    )
    assert response.status_code == 422


def test_projection_drop_does_not_consume_sequence_and_marks_incomplete():
    now = datetime.now(timezone.utc)
    run = SimpleNamespace(
        id=uuid4(), snapshot_id=uuid4(), run_type="agent", started_at=now,
        finished_at=now, status="completed", result="answer",
    )
    ownership = SimpleNamespace(ensure_owned=lambda: None)
    publisher = CapturePublisher()
    session = RealtimeTraceSession(run, ownership, publisher)
    session.observe("context", TraceEvent("ContextAssembled", now, data={}))
    session.observe("llm_started", TraceEvent("LLMCallStarted", now, data={"purpose": "plan"}))
    result = SimpleNamespace(trace=SimpleNamespace(events=[], tools=[]), stop_reason="final")
    terminal = session.append_terminal(run, result=result)
    session.publish(terminal)
    assert [event.sequence for event in session.events] == [0, 1, 2]
    assert terminal.payload["trace_complete"] is False
    assert [event_json(event) for event in publisher.events] == [
        event_json(event) for event in session.events
    ]


def test_redis_publish_failure_keeps_buffer_without_degrading_projection():
    now = datetime.now(timezone.utc)
    run = SimpleNamespace(id=uuid4(), snapshot_id=uuid4(), run_type="agent",
                          started_at=now, finished_at=now, status="completed", result="answer")
    ownership = SimpleNamespace(ensure_owned=lambda: None)

    class BrokenPublisher:
        def publish(self, run_id, event):
            raise ConnectionError("redis unavailable")

    session = RealtimeTraceSession(run, ownership, BrokenPublisher())
    session.observe("llm_started", TraceEvent("LLMCallStarted", now, data={"purpose": "plan"}))
    session.observe("llm_finished", TraceEvent("LLMCallFinished", now, data={
        "purpose": "plan", "success": True, "fallback_used": False,
    }))
    result = SimpleNamespace(trace=SimpleNamespace(events=[
        TraceEvent("LLMCallStarted", now, data={"purpose": "plan"}),
        TraceEvent("LLMCallFinished", now, data={"purpose": "plan"}),
    ], tools=[]), stop_reason="final")
    terminal = session.append_terminal(run, result=result)
    assert terminal.payload["trace_complete"] is True
    assert len(session.events) == 4


def test_running_stream_reports_gap_before_and_after_headers(client, ready_url, application):
    accepted = client.post("/api/v1/agent-runs", json={
        "project_id": ready_url.rsplit("/", 1)[-1], "task": "analyze",
    })
    assert accepted.status_code == 202
    run_id = accepted.json()["run_id"]
    now = datetime.now(timezone.utc)
    start = ApplicationTraceEvent(0, ApplicationEventType.RUN_STARTED, now, None, {
        "run_type": "agent", "snapshot_id": str(uuid4()),
    })
    skipped = ApplicationTraceEvent(2, ApplicationEventType.PLAN_CREATED, now, None, {
        "source": "rule", "fallback_used": False, "steps": [],
    })
    application.state.realtime_redis = ReplayRedis([skipped])
    response = client.get(f"/api/v1/agent-runs/{run_id}/events")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "realtime_trace_gap"

    application.state.realtime_redis = ReplayRedis([start, skipped])
    response = client.get(f"/api/v1/agent-runs/{run_id}/events")
    assert response.status_code == 200
    assert "id: 0\nevent: trace" in response.text
    assert 'event: realtime_trace_gap\ndata: {"code":"realtime_trace_gap"}' in response.text
    assert "id: 2\n" not in response.text


def test_terminal_replay_uses_postgres_when_redis_is_unavailable(
    client, ready_url, application, agent_worker,
):
    run_id, _, stored = _execute(client, ready_url, application, agent_worker)

    class UnavailableRedis:
        async def xrange(self, key):
            raise ConnectionError("redis unavailable")

        async def aclose(self):
            pass

    application.state.realtime_redis = UnavailableRedis()
    response = client.get(f"/api/v1/agent-runs/{run_id}/events")
    assert response.status_code == 200
    assert response.text.count("event: trace") == len(stored)

    response = client.get(
        f"/api/v1/agent-runs/{run_id}/events", headers={"Last-Event-ID": "0"},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "realtime_trace_unavailable"


def test_ownership_loss_stops_further_live_publishes():
    now = datetime.now(timezone.utc)
    run = SimpleNamespace(id=uuid4(), snapshot_id=uuid4(), run_type="agent", started_at=now)
    calls = 0

    def ensure_owned():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("ownership lost")

    publisher = CapturePublisher()
    session = RealtimeTraceSession(
        run, SimpleNamespace(ensure_owned=ensure_owned), publisher,
    )
    session.observe("llm_started", TraceEvent("LLMCallStarted", now, data={"purpose": "plan"}))
    session.observe("llm_finished", TraceEvent("LLMCallFinished", now, data={
        "purpose": "plan", "success": True, "fallback_used": False,
    }))
    assert [event.sequence for event in session.events] == [0, 1, 2]
    assert [event.sequence for event in publisher.events] == [0]
    assert calls == 2


def test_redis_stream_write_sets_ttl_in_same_transaction():
    now = datetime.now(timezone.utc)
    run_id = uuid4()
    event = ApplicationTraceEvent(0, ApplicationEventType.RUN_STARTED, now, None, {
        "run_type": "agent", "snapshot_id": str(uuid4()),
    })
    commands = []

    class Pipeline:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def xadd(self, *args, **kwargs):
            commands.append(("xadd", args, kwargs))

        def expire(self, *args):
            commands.append(("expire", args))

        def execute(self):
            commands.append(("execute",))

    class Client:
        def pipeline(self, *, transaction):
            assert transaction is True
            return Pipeline()

    publisher = RedisStreamPublisher(RedisSettings("redis://localhost:6379/0"))
    publisher._client = Client()
    publisher.publish(run_id, event)
    assert [command[0] for command in commands] == ["xadd", "expire", "execute"]
    assert commands[0][1][0] == stream_key(run_id)
    assert commands[0][2] == {"maxlen": 512, "approximate": True}
    assert commands[1][1] == (stream_key(run_id), 86400)


def test_terminal_projection_failure_preserves_business_completion(
    client, ready_url, application, agent_worker, monkeypatch,
):
    def broken_terminal(*args, **kwargs):
        raise ValueError("invalid observation")

    monkeypatch.setattr(RealtimeTraceSession, "append_terminal", broken_terminal)
    run_id, published, stored = _execute(client, ready_url, application, agent_worker)
    assert application.state.persistence.agent_runs[run_id].status == "completed"
    assert [event.event_type for event in stored] == ["run_started", "run_completed"]
    assert stored[-1].payload["trace_complete"] is False
    assert published[0].event_type == "run_started"
    assert published[-1].event_type != "run_completed"
