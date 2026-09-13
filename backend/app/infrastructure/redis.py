from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from backend.app.config import load_environment


@dataclass(frozen=True)
class RedisSettings:
    url: str

    @classmethod
    def from_environment(cls) -> RedisSettings:
        load_environment()
        value = os.environ.get("REDIS_URL", "").strip()
        if not value:
            raise RuntimeError("REDIS_URL is required.")
        parsed = urlsplit(value)
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
            raise RuntimeError("REDIS_URL must be a valid redis:// or rediss:// URL.")
        return cls(url=value)
