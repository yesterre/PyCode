"""SSE replay and live delivery for v1 public Agent traces."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime
from time import monotonic
from typing import Any, Iterator
from uuid import UUID

from fastapi import Request
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis

from backend.app.core.application_trace import (
    ApplicationEventType, ApplicationTraceEvent, application_event_from_record,
)
from backend.app.core.errors import BackendError
from backend.app.core.models import AgentRunStatus
from backend.app.infrastructure.realtime_trace import event_json, stream_key
from backend.app.infrastructure.redis import RedisSettings
from backend.app.repositories import InMemoryUnitOfWork, SqlAlchemyUnitOfWork


GAP = 'event: realtime_trace_gap\ndata: {"code":"realtime_trace_gap"}\n\n'
TERMINAL = {AgentRunStatus.COMPLETED, AgentRunStatus.FAILED}


@contextmanager
def _unit_of_work(request: Request) -> Iterator[Any]:
    if request.app.state.persistence is not None:
        yield InMemoryUnitOfWork(request.app.state.persistence)
        return
    session = request.app.state.database.open_session()
    try:
        yield SqlAlchemyUnitOfWork(session)
    finally:
        if session.in_transaction():
            session.rollback()
        session.close()


def _snapshot(request: Request, run_id: UUID, owner_id: UUID) -> tuple[Any, list[ApplicationTraceEvent]]:
    with _unit_of_work(request) as uow:
        with uow.transaction():
            run = uow.agent_runs.get_owned(run_id, owner_id)
            events = []
            if run.status in TERMINAL:
                events = [
                    event for record in uow.trace_events.list_for_run(run_id)
                    if (event := application_event_from_record(record)) is not None
                ]
            return run, events


def _frame(event: ApplicationTraceEvent) -> str:
    return f"id: {event.sequence}\nevent: trace\ndata: {event_json(event)}\n\n"


def _decode_entries(entries: Any) -> list[tuple[str, dict[str, Any]]]:
    decoded = []
    for stream_id, fields in entries:
        raw = fields.get(b"event", fields.get("event"))
        if raw is None:
            raise ValueError("Missing stream event")
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {
            "schema_version", "sequence", "event_type", "occurred_at", "tool_name", "payload",
        }:
            raise ValueError("Invalid stream event")
        ApplicationTraceEvent(
            sequence=value["sequence"],
            event_type=ApplicationEventType(value["event_type"]),
            occurred_at=datetime.fromisoformat(value["occurred_at"].replace("Z", "+00:00")),
            tool_name=value["tool_name"],
            payload=value["payload"],
            schema_version=value["schema_version"],
        )
        decoded.append((stream_id.decode() if isinstance(stream_id, bytes) else stream_id, value))
    return decoded


def _canonical(event: dict[str, Any]) -> str:
    fields = ("schema_version", "sequence", "event_type", "occurred_at", "tool_name", "payload")
    return json.dumps({field: event[field] for field in fields}, sort_keys=True,
                      ensure_ascii=False, separators=(",", ":"))


def _prefix_matches(stream: list[tuple[str, dict[str, Any]]],
                    stored: list[ApplicationTraceEvent], sequence: int) -> bool:
    if sequence >= len(stored):
        return False
    prefix = [value for _, value in stream if value["sequence"] <= sequence]
    return len(prefix) == sequence + 1 and all(
        value["sequence"] == index and _canonical(value) == _canonical(json.loads(event_json(stored[index])))
        for index, value in enumerate(prefix)
    )


def _live_event(value: dict[str, Any], expected: int) -> str | None:
    if value.get("sequence") != expected:
        return None
    return f'id: {expected}\nevent: trace\ndata: {json.dumps(value, ensure_ascii=False, separators=(",", ":"))}\n\n'


async def stream_agent_run_events(request: Request, run_id: UUID, *, owner_id: UUID) -> StreamingResponse:
    # Authorize before inspecting replay state or opening the Redis stream.
    run, stored = _snapshot(request, run_id, owner_id)
    raw_id = request.headers.get("last-event-id")
    if raw_id is not None and (
        not raw_id.isascii() or not raw_id.isdecimal() or len(raw_id) > 19
    ):
        raise BackendError("invalid_last_event_id", "Last-Event-ID must be a non-negative integer.")
    last_sequence = int(raw_id) if raw_id is not None else None
    if run.status in TERMINAL and last_sequence is None:
        async def terminal_replay():
            for event in stored:
                if await request.is_disconnected():
                    break
                yield _frame(event)
        return StreamingResponse(terminal_replay(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    redis: Redis | None = request.app.state.realtime_redis
    if redis is None:
        try:
            async with request.app.state.realtime_redis_lock:
                redis = request.app.state.realtime_redis
                if redis is None:
                    settings = RedisSettings.from_environment()
                    redis = Redis.from_url(
                        settings.url,
                        socket_connect_timeout=settings.connect_timeout_seconds,
                        socket_timeout=max(settings.socket_timeout_seconds, 8.0),
                        max_connections=settings.realtime_sse_max_connections,
                    )
                    request.app.state.realtime_redis = redis
        except Exception as exc:
            raise BackendError("realtime_trace_unavailable", "Realtime trace is unavailable.") from exc
    key = stream_key(run_id)
    try:
        replay = _decode_entries(await redis.xrange(key))
    except (ValueError, KeyError, TypeError) as exc:
        raise BackendError("realtime_trace_gap", "Realtime trace history has a gap.") from exc
    except Exception as exc:
        raise BackendError("realtime_trace_unavailable", "Realtime trace is unavailable.") from exc

    if last_sequence is not None:
        if run.status in TERMINAL:
            if not _prefix_matches(replay, stored, last_sequence):
                raise BackendError("realtime_trace_gap", "Realtime trace history has a gap.")
            async def terminal_resume():
                for event in stored:
                    if event.sequence > last_sequence and not await request.is_disconnected():
                        yield _frame(event)
            return StreamingResponse(terminal_resume(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        prefix = [item for item in replay if item[1]["sequence"] <= last_sequence]
        if len(prefix) != last_sequence + 1 or any(
            item[1]["sequence"] != index for index, item in enumerate(prefix)
        ):
            raise BackendError("realtime_trace_gap", "Realtime trace history has a gap.")
    elif replay and replay[0][1]["sequence"] != 0:
        raise BackendError("realtime_trace_gap", "Realtime trace history has a gap.")

    async def live():
        expected = 0 if last_sequence is None else last_sequence + 1
        delivered = [value for _, value in replay if value["sequence"] < expected]
        cursor = "0-0"
        last_heartbeat = monotonic()
        try:
            for stream_id, value in replay:
                cursor = stream_id
                if value["sequence"] < expected:
                    continue
                frame = _live_event(value, expected)
                if frame is None:
                    yield GAP
                    return
                yield frame
                delivered.append(value)
                expected += 1
                if value.get("event_type") in {"run_completed", "run_failed"}:
                    return
            while not await request.is_disconnected():
                try:
                    records = await redis.xread({key: cursor}, count=100, block=5000)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    yield GAP
                    return
                if records:
                    try:
                        entries = _decode_entries(records[0][1])
                    except Exception:
                        yield GAP
                        return
                    for stream_id, value in entries:
                        cursor = stream_id
                        frame = _live_event(value, expected)
                        if frame is None:
                            yield GAP
                            return
                        yield frame
                        delivered.append(value)
                        expected += 1
                        if value.get("event_type") in {"run_completed", "run_failed"}:
                            return
                terminal_run, terminal_events = _snapshot(request, run_id, owner_id)
                if terminal_run.status in TERMINAL:
                    if expected > len(terminal_events) or len(delivered) != expected or any(
                        item["sequence"] != index or
                        _canonical(item) != _canonical(json.loads(event_json(terminal_events[index])))
                        for index, item in enumerate(delivered)
                    ):
                        yield GAP
                        return
                    for event in terminal_events[expected:]:
                        yield _frame(event)
                    return
                if not records and monotonic() - last_heartbeat >= 15:
                    yield ": ping\n\n"
                    last_heartbeat = monotonic()
        except asyncio.CancelledError:
            return

    return StreamingResponse(live(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
