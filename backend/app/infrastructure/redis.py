from __future__ import annotations

import os
from dataclasses import dataclass
from threading import Lock
from urllib.parse import urlsplit
from uuid import UUID

from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry

from backend.app.config import load_environment
from backend.app.core.errors import BackendError


_RATE_LIMIT = """
local used = tonumber(redis.call('GET', KEYS[1]) or '0')
if used >= tonumber(ARGV[1]) then return 0 end
used = redis.call('INCR', KEYS[1])
if used == 1 then redis.call('EXPIRE', KEYS[1], ARGV[2]) end
return 1
"""


class RedisRateLimiter:
    """Per-user/operation windows anchored to their first admitted request."""

    def __init__(self, client: Redis | None = None) -> None:
        self.client = client
        self._guard = Lock()

    def check(self, user_id: UUID, operation: str, limit: int, window_seconds: int) -> None:
        key = f"pycode:rate:v1:{user_id}:{operation}"
        try:
            if self.client is None:
                with self._guard:
                    if self.client is None:
                        settings = RedisSettings.from_environment()
                        client = Redis.from_url(
                            settings.url,
                            socket_connect_timeout=min(settings.connect_timeout_seconds, 1.0),
                            socket_timeout=min(settings.socket_timeout_seconds, 1.0),
                            retry=Retry(NoBackoff(), 0), max_connections=32,
                        )
                        # redis-py URL options override kwargs. Apply admission
                        # bounds before the pool opens its first connection.
                        client.connection_pool.connection_kwargs.update(
                            socket_connect_timeout=min(settings.connect_timeout_seconds, 1.0),
                            socket_timeout=min(settings.socket_timeout_seconds, 1.0),
                            retry=Retry(NoBackoff(), 0), retry_on_timeout=False, retry_on_error=[],
                        )
                        client.connection_pool.max_connections = 32
                        self.client = client
            admitted = self.client.eval(_RATE_LIMIT, 1, key, limit, window_seconds)
        except (RedisError, OSError, ValueError, RuntimeError):
            raise BackendError("admission_unavailable", "Admission control is unavailable.") from None
        if admitted == 0:
            raise BackendError("rate_limited", "Request rate limit exceeded. Try again later.")
        if admitted != 1:
            raise BackendError("admission_unavailable", "Admission control is unavailable.")

    def close(self) -> None:
        if self.client is not None:
            self.client.close()


@dataclass(frozen=True)
class RedisSettings:
    url: str
    connect_timeout_seconds: float = 5.0
    socket_timeout_seconds: float = 5.0
    realtime_stream_maxlen: int = 512
    realtime_stream_ttl_seconds: int = 86400
    realtime_sse_max_connections: int = 100

    @classmethod
    def from_environment(cls) -> RedisSettings:
        load_environment()
        value = os.environ.get("REDIS_URL", "").strip()
        if not value:
            raise RuntimeError("REDIS_URL is required.")
        parsed = urlsplit(value)
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
            raise RuntimeError("REDIS_URL must be a valid redis:// or rediss:// URL.")
        connect_timeout = float(os.environ.get(
            "PYCODE_REDIS_CONNECT_TIMEOUT_SECONDS", "5",
        ))
        socket_timeout = float(os.environ.get(
            "PYCODE_REDIS_SOCKET_TIMEOUT_SECONDS", "5",
        ))
        if connect_timeout <= 0 or socket_timeout <= 0:
            raise RuntimeError("Redis connect and socket timeouts must be positive.")
        maxlen = int(os.environ.get("PYCODE_REALTIME_STREAM_MAXLEN", "512"))
        ttl = int(os.environ.get("PYCODE_REALTIME_STREAM_TTL_SECONDS", "86400"))
        connections = int(os.environ.get("PYCODE_REALTIME_SSE_MAX_CONNECTIONS", "100"))
        if min(maxlen, ttl, connections) <= 0:
            raise RuntimeError("Realtime Redis limits must be positive.")
        return cls(
            url=value,
            connect_timeout_seconds=connect_timeout,
            socket_timeout_seconds=socket_timeout,
            realtime_stream_maxlen=maxlen,
            realtime_stream_ttl_seconds=ttl,
            realtime_sse_max_connections=connections,
        )
