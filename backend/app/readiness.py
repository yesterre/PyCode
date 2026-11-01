"""Dependency readiness; never execute business work or consume a user quota."""
from threading import Lock

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from redis import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from backend.app.config import AuthSettings
from backend.app.infrastructure.redis import RedisSettings


class ReadinessProbe:
    def __init__(self, database):
        self.database = database
        self.client = None
        self.guard = Lock()

    def check(self, auth_settings=None):
        if not self.guard.acquire(blocking=False):
            return {"probe": False}
        try:
            try:
                auth_settings or AuthSettings.from_environment()
                configured = True
            except Exception:
                configured = False
            postgres = self.database.probe() if self.database is not None else False
            try:
                if self.client is None:
                    settings = RedisSettings.from_environment()
                    client = Redis.from_url(settings.url, socket_connect_timeout=1, socket_timeout=1,
                                            retry=Retry(NoBackoff(), 0), max_connections=1)
                    # URL query options must not override the probe's I/O bounds.
                    client.connection_pool.connection_kwargs.update(
                        socket_connect_timeout=1, socket_timeout=1, retry=Retry(NoBackoff(), 0),
                        retry_on_timeout=False, retry_on_error=[])
                    client.connection_pool.max_connections = 1
                    self.client = client
                redis = self.client.ping() is True
            except Exception:
                redis = False
            return {"configuration": configured, "postgresql": postgres, "redis": redis}
        finally:
            self.guard.release()

    def close(self):
        if self.client is not None:
            self.client.close()


router = APIRouter()


@router.get("/ready", include_in_schema=False)
def ready(request: Request):
    checks = request.app.state.readiness.check(request.app.state.auth_settings)
    success = all(checks.values())
    return JSONResponse({"status": "ok" if success else "not_ready", "checks": checks},
                        status_code=200 if success else 503, headers={"Cache-Control": "no-store"})
