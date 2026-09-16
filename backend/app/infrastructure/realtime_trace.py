"""Dedicated Redis Stream transport for public Agent trace events."""

from __future__ import annotations

import json
from uuid import UUID

from redis import Redis

from backend.app.core.application_trace import ApplicationTraceEvent, format_utc
from backend.app.infrastructure.redis import RedisSettings


def stream_key(run_id: UUID) -> str:
    return f"pycode:agent-runs:{run_id}:events"


def event_json(event: ApplicationTraceEvent) -> str:
    value = event.to_api_dict()
    value["occurred_at"] = format_utc(event.occurred_at)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class RedisStreamPublisher:
    def __init__(self, settings: RedisSettings | None = None) -> None:
        self.settings = settings or RedisSettings.from_environment()
        self._client: Redis | None = None

    def publish(self, run_id: UUID, event: ApplicationTraceEvent) -> None:
        if self._client is None:
            self._client = Redis.from_url(
                self.settings.url,
                socket_connect_timeout=self.settings.connect_timeout_seconds,
                socket_timeout=self.settings.socket_timeout_seconds,
            )
        key = stream_key(run_id)
        with self._client.pipeline(transaction=True) as pipe:
            pipe.xadd(key, {"event": event_json(event)},
                      maxlen=self.settings.realtime_stream_maxlen, approximate=True)
            pipe.expire(key, self.settings.realtime_stream_ttl_seconds)
            pipe.execute()
