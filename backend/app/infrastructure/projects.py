from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Event, RLock, Thread
from time import monotonic
from typing import Callable, Iterator, Protocol
from uuid import UUID, uuid4

from redis import Redis
from redis.exceptions import RedisError

from backend.app.core.errors import BackendError
from backend.app.infrastructure.redis import RedisSettings


logger = logging.getLogger("pycode.backend.locks")
PROJECT_LOCK_PREFIX = "pycode:lock:project:"
COMPARE_AND_DELETE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""
COMPARE_AND_RENEW = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""


class ProjectLockBackend(Protocol):
    def acquire(self, key: str, token: str, ttl_ms: int) -> bool: ...

    def owns(self, key: str, token: str) -> bool: ...

    def renew(self, key: str, token: str, ttl_ms: int) -> bool: ...

    def release(self, key: str, token: str) -> bool: ...


class RedisProjectLockBackend:
    """Redis token lock using atomic compare-and-delete/renew Lua scripts."""

    def __init__(
        self,
        redis_url: str | None = None,
        *,
        client: Redis | None = None,
    ) -> None:
        self._redis_url = redis_url
        self._client_instance = client
        self._guard = RLock()

    def acquire(self, key: str, token: str, ttl_ms: int) -> bool:
        try:
            return bool(self._client().set(key, token, nx=True, px=ttl_ms))
        except (RedisError, OSError, RuntimeError, ValueError) as exc:
            raise self._unavailable() from exc

    def owns(self, key: str, token: str) -> bool:
        try:
            value = self._client().get(key)
        except (RedisError, OSError, RuntimeError, ValueError) as exc:
            raise self._unavailable() from exc
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        return value == token

    def renew(self, key: str, token: str, ttl_ms: int) -> bool:
        try:
            return bool(self._client().eval(
                COMPARE_AND_RENEW, 1, key, token, ttl_ms,
            ))
        except (RedisError, OSError, RuntimeError, ValueError) as exc:
            raise self._unavailable() from exc

    def release(self, key: str, token: str) -> bool:
        try:
            return bool(self._client().eval(COMPARE_AND_DELETE, 1, key, token))
        except (RedisError, OSError, RuntimeError, ValueError) as exc:
            raise self._unavailable() from exc

    def _client(self) -> Redis:
        if self._client_instance is None:
            with self._guard:
                if self._client_instance is None:
                    settings = (
                        RedisSettings(self._redis_url)
                        if self._redis_url is not None
                        else RedisSettings.from_environment()
                    )
                    self._client_instance = Redis.from_url(
                        settings.url,
                        decode_responses=True,
                        socket_connect_timeout=settings.connect_timeout_seconds,
                        socket_timeout=settings.socket_timeout_seconds,
                    )
        return self._client_instance

    @staticmethod
    def _unavailable() -> BackendError:
        return BackendError(
            "project_lock_unavailable", "Project coordination is unavailable.",
        )


class InMemoryProjectLockBackend:
    """Process-local semantic test double; production uses Redis."""

    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock
        self._guard = RLock()
        self._locks: dict[str, tuple[str, float]] = {}

    def acquire(self, key: str, token: str, ttl_ms: int) -> bool:
        with self._guard:
            self._discard_expired(key)
            if key in self._locks:
                return False
            self._locks[key] = (token, self._clock() + ttl_ms / 1000)
            return True

    def owns(self, key: str, token: str) -> bool:
        with self._guard:
            self._discard_expired(key)
            current = self._locks.get(key)
            return current is not None and current[0] == token

    def renew(self, key: str, token: str, ttl_ms: int) -> bool:
        with self._guard:
            self._discard_expired(key)
            current = self._locks.get(key)
            if current is None or current[0] != token:
                return False
            self._locks[key] = (token, self._clock() + ttl_ms / 1000)
            return True

    def release(self, key: str, token: str) -> bool:
        with self._guard:
            self._discard_expired(key)
            current = self._locks.get(key)
            if current is None or current[0] != token:
                return False
            del self._locks[key]
            return True

    def _discard_expired(self, key: str) -> None:
        current = self._locks.get(key)
        if current is not None and current[1] <= self._clock():
            del self._locks[key]


@dataclass
class ProjectLockHandle:
    backend: ProjectLockBackend
    key: str
    token: str
    ttl_ms: int
    _lost: Event = field(default_factory=Event, repr=False)

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def mark_lost(self) -> None:
        self._lost.set()

    def ensure_owned(self) -> None:
        if self.lost:
            raise self._lost_error()
        try:
            owned = self.backend.owns(self.key, self.token)
        except Exception as exc:
            self.mark_lost()
            raise self._lost_error() from exc
        if not owned:
            self.mark_lost()
            raise self._lost_error()

    @staticmethod
    def _lost_error() -> BackendError:
        return BackendError(
            "project_lock_lost",
            "Project operation ownership was lost before completion.",
        )


class ProjectOperationLocks:
    """One renewable project lock around each workspace-mutating operation."""

    def __init__(
        self,
        backend: ProjectLockBackend | None = None,
        *,
        ttl_seconds: float = 300,
        renewal_interval_seconds: float | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("Project lock TTL must be positive.")
        renewal = renewal_interval_seconds or min(60.0, ttl_seconds / 3)
        if renewal <= 0 or renewal >= ttl_seconds:
            raise ValueError("Project lock renewal interval must be below its TTL.")
        self.backend = backend or InMemoryProjectLockBackend()
        self.ttl_ms = max(1, int(ttl_seconds * 1000))
        self.renewal_interval_seconds = renewal

    @staticmethod
    def key_for(project_id: UUID) -> str:
        return f"{PROJECT_LOCK_PREFIX}{project_id}"

    @contextmanager
    def operation(self, project_id: UUID) -> Iterator[ProjectLockHandle]:
        key = self.key_for(project_id)
        token = uuid4().hex
        if not self.backend.acquire(key, token, self.ttl_ms):
            raise BackendError(
                "project_busy", "Another operation is running for this project.",
            )

        handle = ProjectLockHandle(self.backend, key, token, self.ttl_ms)
        stopped = Event()

        def renew() -> None:
            while not stopped.wait(self.renewal_interval_seconds):
                try:
                    renewed = self.backend.renew(key, token, self.ttl_ms)
                except Exception:
                    renewed = False
                    logger.exception("Project lock renewal failed key=%s", key)
                if not renewed:
                    handle.mark_lost()
                    logger.error("Project lock ownership lost key=%s", key)
                    return

        renewal_thread = Thread(
            target=renew,
            name=f"pycode-project-lock-{project_id}",
            daemon=True,
        )
        renewal_thread.start()
        block_completed = False
        try:
            yield handle
            handle.ensure_owned()
            block_completed = True
        finally:
            stopped.set()
            renewal_thread.join(timeout=1)
            try:
                released = self.backend.release(key, token)
            except Exception:
                logger.exception("Project lock release failed key=%s", key)
            else:
                if not released and block_completed:
                    logger.error("Project lock was not owned during release key=%s", key)
