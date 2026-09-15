from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from backend.app.config import load_environment


@dataclass(frozen=True)
class RedisSettings:
    url: str
    connect_timeout_seconds: float = 5.0
    socket_timeout_seconds: float = 5.0

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
        return cls(
            url=value,
            connect_timeout_seconds=connect_timeout,
            socket_timeout_seconds=socket_timeout,
        )
