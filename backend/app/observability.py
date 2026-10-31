"""Bounded public log fields and streaming-safe HTTP request correlation."""

from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
from time import perf_counter
from uuid import UUID, uuid4

from starlette.datastructures import MutableHeaders

from backend.app.core.errors import ERROR_CATALOG, error_spec


request_context: ContextVar[dict | None] = ContextVar("pycode_request", default=None)
logger = logging.getLogger("pycode.backend")
HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"}


def safe_error_code(code: str) -> str:
    return code if code in ERROR_CATALOG or code in {
        "internal_error", "permission_denied", "path_not_found", "invalid_source",
        "background_task_failed", "agent_run_failed", "request_validation",
    } else "internal_error"


def log_event(event: str, *, level=logging.INFO, **fields) -> None:
    context = request_context.get()
    if context is not None:
        context.update({key: value for key, value in fields.items() if key in {"task_id", "run_id"}})
    logger.log(level, event, extra={**(context or {}), **fields})


class JsonFormatter(logging.Formatter):
    def format(self, record):
        context = request_context.get() or {}
        result = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname, "event": record.getMessage(),
            "request_id": getattr(record, "request_id", context.get("request_id")),
        }
        # No arbitrary extras, exception text, bodies, URLs or query strings.
        for key in ("route", "method", "status", "duration_seconds", "task_id",
                    "run_id", "task_type", "error_code", "error_category",
                    "worker_id", "attempt_count", "execution_token_prefix", "lease_expires_at"):
            value = getattr(record, key, context.get(key))
            if value is not None:
                result[key] = value.isoformat() if isinstance(value, datetime) else str(value) if isinstance(value, UUID) else value
        if "error_code" in result and "error_category" not in result:
            result["error_category"] = error_spec(result["error_code"]).kind.value
        return json.dumps(result, ensure_ascii=True, allow_nan=False)


class PublicLogFilter(logging.Filter):
    def filter(self, record):
        # Also clear these on the record before host/root handlers see it.
        record.exc_info = record.exc_text = record.stack_info = None
        if record.name == "uvicorn.access":
            # Uvicorn's AccessFormatter unpacks exactly five args. Preserve its
            # contract while dropping raw client/path/query and arbitrary text.
            args = record.args if isinstance(record.args, tuple) and len(record.args) == 5 else (None,) * 5
            method = args[1] if args[1] in HTTP_METHODS else "OTHER"
            version = args[3] if args[3] in {"1.0", "1.1", "2", "3"} else "1.1"
            status = args[4] if type(args[4]) is int and 100 <= args[4] <= 599 else 500
            record.msg = '%s - "%s %s HTTP/%s" %d'
            record.args = ("-", method, "[redacted]", version, status)
        elif record.name == "uvicorn.error" and record.getMessage().startswith("Exception in ASGI application"):
            record.msg, record.args = "asgi_error", ()
        elif record.name == "celery.app.trace":
            # Celery failure templates include repr(exception) and task args.
            record.msg, record.args = "celery_task", ()
        return True


def configure_logging() -> None:
    # Own only the backend handler; leave host/pytest logging configuration alone.
    if not any(getattr(handler, "pycode_json", False) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler.pycode_json = True
        handler.addFilter(PublicLogFilter())
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    for name in ("uvicorn.access", "uvicorn.error", "celery.app.trace"):
        target = logging.getLogger(name)
        if not any(isinstance(item, PublicLogFilter) for item in target.filters):
            target.addFilter(PublicLogFilter())


def request_id(value: str | None) -> str:
    try:
        # Strict canonical UUIDs prevent credentials/arbitrary text becoming IDs.
        if value is not None and len(value) == 36 and str(UUID(value)) == value.lower():
            return str(UUID(value))
    except (ValueError, AttributeError):
        pass
    return str(uuid4())


class RequestObservabilityMiddleware:
    def __init__(self, app, metrics):
        self.app, self.metrics = app, metrics

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        incoming = [value for key, value in scope.get("headers", []) if key.lower() == b"x-request-id"]
        identity = request_id(incoming[0].decode("latin-1") if len(incoming) == 1 else None)
        context = {"request_id": identity}
        scope.setdefault("state", {})["observation"] = context
        token = request_context.set(context)
        started, status, response_started = perf_counter(), 500, False

        async def observed_send(message):
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                status, response_started = message["status"], True
                MutableHeaders(scope=message)["X-Request-ID"] = identity
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        except BaseException as exc:
            context.setdefault("error_code", safe_error_code(getattr(exc, "code", "internal_error")))
            if not response_started:
                status = 500
            raise
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            method = scope["method"] if scope["method"] in HTTP_METHODS else "OTHER"
            duration = max(0.0, perf_counter() - started)
            context.update(route=route, method=method, status=status, duration_seconds=duration)
            code = context.get("error_code")
            context["error_category"] = (
                error_spec(code).kind.value if code is not None
                else "http_error" if status >= 400 else "none"
            )
            try:
                self.metrics.observe(route, method, status, duration)
                log_event("http_request", **context)
            finally:
                request_context.reset(token)
