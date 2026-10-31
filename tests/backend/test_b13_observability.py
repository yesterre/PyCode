import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import logging
import sys
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import event

from backend.app.api.dependencies import get_db_session
from backend.app.config import ObservabilitySettings
from backend.app.core.errors import BackendError
from backend.app.db.models import BackgroundTaskORM
from backend.app.metrics import RequestMetrics, task_metrics
from backend.app.observability import (
    JsonFormatter, PublicLogFilter, RequestObservabilityMiddleware, configure_logging, request_context,
)
from test_b01_users import uow_factory
from test_b02_auth import auth_context
from test_b04_project_derived_auth import resources
from test_b05_async_auth import submit
from test_b07_worker_isolation import mutate, ready, workers
from test_postgresql import postgresql_engine


METRICS_TOKEN = "b13-independent-scrape-credential-" * 2
PRIVATE = "password-JWT-private-prompt-source-DO-NOT-LOG"


def records(caplog, event_name):
    formatter = JsonFormatter()
    return [json.loads(formatter.format(record)) for record in caplog.records
            if record.name == "pycode.backend" and record.getMessage() == event_name]


@pytest.mark.parametrize("supplied", [None, "", "not-a-uuid", PRIVATE, "x" * 5000, str(uuid4()), str(uuid4()).upper()])
def test_request_id_is_canonical_and_does_not_reflect_arbitrary_input(application, caplog, supplied):
    with TestClient(application) as client:
        response = client.get("/health?access_token=" + PRIVATE,
                              headers={"X-Request-ID": supplied} if supplied is not None else {})
    identity = response.headers["x-request-id"]
    assert str(UUID(identity)) == identity
    if supplied and len(supplied) == 36:
        assert identity == supplied.lower()
    assert response.json() == {"status": "ok"}
    entry = records(caplog, "http_request")[-1]
    assert entry["request_id"] == identity
    assert (entry["route"], entry["status"], entry["method"]) == ("/health", 200, "GET")
    assert entry["duration_seconds"] >= 0 and entry["error_category"] == "none"
    assert PRIVATE not in caplog.text
    assert request_context.get() is None


def test_duplicate_request_id_headers_are_replaced(application):
    identity = str(uuid4())
    with TestClient(application) as client:
        response = client.get("/health", headers=[("X-Request-ID", identity), ("X-Request-ID", identity)])
    assert str(UUID(response.headers["x-request-id"])) != identity


def test_concurrent_requests_keep_context_in_async_and_sync_endpoints(application, caplog):
    @application.get("/probe/{item}")
    async def probe(item: int):
        before = request_context.get()["request_id"]
        await asyncio.sleep(0.01)
        return {"before": before, "after": request_context.get()["request_id"]}

    @application.get("/sync-probe")
    def sync_probe():
        return {"identity": request_context.get()["request_id"]}

    identities = [str(uuid4()) for _ in range(20)]

    async def requests():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
            return await asyncio.gather(*[
                client.get(f"/probe/{index}", headers={"X-Request-ID": identity})
                for index, identity in enumerate(identities)
            ])

    responses = asyncio.run(requests())
    for identity, response in zip(identities, responses):
        assert response.json() == {"before": identity, "after": identity}
    with TestClient(application) as client:
        with ThreadPoolExecutor(max_workers=4) as executor:
            responses = list(executor.map(lambda identity: client.get("/sync-probe", headers={"X-Request-ID": identity}), identities))
    assert [response.json()["identity"] for response in responses] == identities
    entries = records(caplog, "http_request")
    assert len(entries) == 40
    assert {entry["route"] for entry in entries} == {"/probe/{item}", "/sync-probe"}
    assert request_context.get() is None


@pytest.mark.parametrize("unexpected", [False, True])
def test_error_logging_is_sanitized_and_500_keeps_request_id(application, caplog, unexpected):
    @application.get("/failure/{item}")
    def failure(item: str):
        if unexpected:
            raise RuntimeError(PRIVATE)
        raise BackendError("database_unavailable", "The database is unavailable.") from RuntimeError(PRIVATE)

    identity = str(uuid4())
    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get("/failure/" + PRIVATE + "?token=" + PRIVATE,
                              headers={"Authorization": "Bearer " + PRIVATE, "X-Request-ID": identity})
    assert response.status_code == (500 if unexpected else 503)
    assert response.headers["x-request-id"] == identity
    assert PRIVATE not in response.text + caplog.text
    entry = records(caplog, "http_request")[-1]
    assert entry["route"] == "/failure/{item}" and entry["request_id"] == identity
    assert entry["error_code"] == ("internal_error" if unexpected else "database_unavailable")
    assert all(record.exc_info is None for record in caplog.records if record.name.startswith("pycode.backend"))


def test_public_host_and_worker_logs_do_not_dump_tracebacks_queries_or_task_args(caplog):
    configure_logging()
    try:
        raise RuntimeError(PRIVATE)
    except RuntimeError:
        for name in ("pycode.backend.tasks", "uvicorn.error", "celery.app.trace"):
            message = "Exception in ASGI application" if name == "uvicorn.error" else "task_failure"
            logging.getLogger(name).error(message, exc_info=True)
        logging.getLogger("celery.app.trace").error("Task %(name)s failed: %(exc)s", {"name": "agent", "exc": PRIVATE})
        logging.getLogger("uvicorn.access").warning('%s - "%s %s HTTP/%s" %d', "127.0.0.1", "GET", "/events?token=" + PRIVATE, "1.1", 401)
    assert PRIVATE not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_json_formatter_whitelists_extras_and_logging_setup_is_idempotent():
    for _ in range(3):
        configure_logging()
    handlers = [handler for handler in logging.getLogger("pycode.backend").handlers if getattr(handler, "pycode_json", False)]
    assert len(handlers) == 1 and isinstance(handlers[0].formatter, JsonFormatter)
    identity = uuid4()
    record = logging.LogRecord("pycode.backend", logging.ERROR, "", 0, "task_failed", (), None)
    record.task_id, record.password, record.prompt = identity, PRIVATE, PRIVATE
    record.error_code = "model_timeout"
    formatted = handlers[0].formatter.format(record)
    data = json.loads(formatted)
    assert data["task_id"] == str(identity) and data["error_category"] == "timeout"
    assert PRIVATE not in formatted


@pytest.mark.parametrize("method,version,expected_method,expected_version", [
    ("GET", "1.1", "GET", "1.1"), (PRIVATE, PRIVATE, "OTHER", "1.1"), ("POST", "1.0", "POST", "1.0"),
])
def test_uvicorn_access_formatter_retains_its_argument_contract(method, version, expected_method, expected_version):
    from uvicorn.logging import AccessFormatter

    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               (PRIVATE, method, "/events?token=" + PRIVATE, version, 401), None)
    assert PublicLogFilter().filter(record)
    formatter = AccessFormatter(fmt='%(client_addr)s "%(request_line)s" %(status_code)s', use_colors=False)
    rendered = formatter.format(record)
    assert f"{expected_method} [redacted] HTTP/{expected_version}" in rendered
    assert "401 Unauthorized" in rendered
    assert PRIVATE not in rendered


@pytest.mark.parametrize("name", ["uvicorn.error", "celery.app.trace"])
def test_native_host_error_formatters_do_not_restore_private_details(name):
    from celery.app.log import TaskFormatter
    from uvicorn.logging import DefaultFormatter

    try:
        raise RuntimeError(PRIVATE)
    except RuntimeError:
        message, args = ("Exception in ASGI application", ()) if name == "uvicorn.error" else ("Task failed: %s", (PRIVATE,))
        record = logging.LogRecord(name, logging.ERROR, "", 0, message, args, sys.exc_info())
    assert PublicLogFilter().filter(record)
    formatter = (DefaultFormatter("%(message)s", use_colors=False) if name == "uvicorn.error"
                 else TaskFormatter("%(task_name)s[%(task_id)s] %(message)s", use_color=False))
    rendered = formatter.format(record)
    assert PRIVATE not in rendered and "Traceback" not in rendered


def test_sse_chunks_are_forwarded_without_buffering_or_modification():
    chunks = []
    wire = [b"id: 1-0\nevent: run.started\ndata: {\"sequence\":0}\n\n", b": keepalive\n\n"]

    async def stream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
        for chunk in wire:
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
            assert chunks[-1]["body"] == chunk
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def send(message):
        chunks.append(message)

    async def receive():
        pytest.fail("Observation middleware must not consume the request or disconnect channel")

    metrics = RequestMetrics()
    asyncio.run(RequestObservabilityMiddleware(stream, metrics)({"type": "http", "method": "GET", "headers": []}, receive, send))
    assert [message["body"] for message in chunks[1:]] == [*wire, b""]
    assert dict(chunks[0]["headers"])[b"content-type"] == b"text/event-stream"
    assert b"x-request-id" in dict(chunks[0]["headers"])


@pytest.mark.parametrize("enabled,credential,status", [
    (False, None, 404), (False, METRICS_TOKEN, 404), (True, None, 401), (True, PRIVATE, 401),
])
def test_metrics_authorization_precedes_database_access(application, enabled, credential, status):
    application.state.observability_settings = ObservabilitySettings(METRICS_TOKEN if enabled else "")

    def forbidden():
        pytest.fail("Denied metrics scrape must not open a database session")

    application.dependency_overrides[get_db_session] = forbidden
    with TestClient(application) as client:
        response = client.get("/metrics?token=" + METRICS_TOKEN,
                              headers={"Authorization": "Bearer " + credential} if credential else {})
    assert response.status_code == status


def test_metrics_have_finite_labels_and_no_private_identifiers(auth_context):
    context = auth_context
    context.app.state.observability_settings = ObservabilitySettings(METRICS_TOKEN)
    for _ in range(20):
        assert context.client.get("/" + str(uuid4()) + "?token=" + PRIVATE).status_code == 404
    response = context.client.get("/metrics", headers={"Authorization": "Bearer " + METRICS_TOKEN})
    assert response.status_code == 200, response.text
    assert "text/plain; version=0.0.4" in response.headers["content-type"]
    assert response.headers["cache-control"] == "no-store"
    assert 'route="unmatched",method="GET",status_class="4xx"} 20' in response.text
    assert 'le="+Inf"} 20' in response.text
    assert 'pycode_background_tasks{task_type="agent_run",status="completed"} 0' in response.text
    for forbidden in (PRIVATE, METRICS_TOKEN, "user_id", "project_id", "run_id", "task_id", "request_id"):
        assert forbidden not in response.text
    assert "/metrics" not in context.client.get("/openapi.json").json()["paths"]


def test_metrics_database_failure_is_explicit_and_does_not_break_health(auth_context, monkeypatch, caplog):
    context = auth_context
    context.app.state.observability_settings = ObservabilitySettings(METRICS_TOKEN)

    def unavailable(self):
        raise BackendError("database_unavailable", "The database is unavailable.") from RuntimeError(PRIVATE)

    with context.uow_factory() as uow:
        monkeypatch.setattr(type(uow.background_tasks), "metrics_snapshot", unavailable)
    response = context.client.get("/metrics", headers={"Authorization": "Bearer " + METRICS_TOKEN})
    assert response.status_code == 503 and response.json()["detail"]["code"] == "database_unavailable"
    assert context.client.get("/health").json() == {"status": "ok"}
    assert PRIVATE not in caplog.text


def test_durable_task_metrics_are_equivalent_and_one_aggregate_query(uow_factory):
    now = datetime.now(timezone.utc)
    with uow_factory() as uow, uow.transaction():
        for status, wait in [("completed", 7), ("failed", 3), ("running", -1), ("queued", None)]:
            task = uow.background_tasks.create("health")
            mutate(uow, "background_tasks", BackgroundTaskORM, task.id,
                   status=status, created_at=now - timedelta(seconds=100),
                   started_at=None if wait is None else now - timedelta(seconds=100 - wait),
                   finished_at=now if status in {"completed", "failed"} else None,
                   error_code="internal_error" if status == "failed" else None,
                   error_message="safe failure" if status == "failed" else None)
        task = uow.background_tasks.create(PRIVATE)
        mutate(uow, "background_tasks", BackgroundTaskORM, task.id, created_at=now - timedelta(seconds=50))
    statements = []

    def capture(connection, cursor, statement, *args):
        statements.append(statement)

    with uow_factory() as uow, uow.transaction():
        engine = getattr(getattr(uow, "session", None), "bind", None)
        if engine is not None:
            event.listen(engine, "before_cursor_execute", capture)
        try:
            rows = uow.background_tasks.metrics_snapshot()
        finally:
            if engine is not None:
                event.remove(engine, "before_cursor_execute", capture)
    indexed = {(row["task_type"], row["status"]): row for row in rows}
    assert len(rows) == 5 and sum(row["count"] for row in rows) == 5
    assert sum(row["started_count"] for row in rows) == 3
    assert sum(row["wait_sum"] for row in rows) == pytest.approx(10)
    assert 99 <= indexed["health", "queued"]["oldest_age"] < 110
    assert 49 <= indexed["other", "queued"]["oldest_age"] < 60
    assert PRIVATE not in "\n".join(task_metrics(rows))
    if engine is not None:
        assert len(statements) == 1 and "GROUP BY" in statements[0]


def test_request_submission_and_worker_join_by_durable_task_and_run_ids(resources, workers, caplog):
    r, context = resources, resources.context
    ready(r, workers)
    identity, replay_identity = str(uuid4()), str(uuid4())
    headers = {**r.headers, "X-Request-ID": identity}
    accepted = submit(context, r.own, headers, key="b13-idempotency", task=PRIVATE)
    assert accepted.status_code == 202, accepted.text
    data = accepted.json()
    replay = submit(context, r.own, {**r.headers, "X-Request-ID": replay_identity}, key="b13-idempotency", task=PRIVATE)
    assert replay.json() == data
    assert request_context.get() is None
    workers.agent(data["task_id"])
    workers.agent(data["task_id"])
    submissions = [row for row in records(caplog, "task_submitted") if row.get("run_id") == data["run_id"]]
    assert {row["request_id"] for row in submissions} == {identity, replay_identity}
    claims = [row for row in records(caplog, "task_claimed") if row.get("task_id") == data["task_id"]]
    assert len(claims) == 1 and claims[0]["run_id"] == data["run_id"]
    assert claims[0]["request_id"] is None  # Worker needs task IDs, never the browser JWT.
    completed = [row for row in records(caplog, "task_completed") if row.get("task_id") == data["task_id"]]
    assert len(completed) == 1
    with context.uow_factory() as uow, uow.transaction():
        rows = uow.background_tasks.metrics_snapshot()
        task = uow.background_tasks.get(UUID(data["task_id"]))
        assert task.attempt_count == 1 and task.status == "completed"
    assert next(row for row in rows if row["task_type"] == "agent_run")["count"] == 1
    # Existing durable Trace replay remains owner-only and is not rewritten by observation.
    trace = context.client.get(f"/api/v1/agent-runs/{data['run_id']}/trace", headers=r.headers)
    assert trace.status_code == 200 and trace.json()["events"]
    stream = context.client.get(f"/api/v1/agent-runs/{data['run_id']}/events", headers=r.headers)
    assert stream.status_code == 200 and "text/event-stream" in stream.headers["content-type"]
    assert len([line for line in stream.text.splitlines() if line.startswith("data: ")]) == len(trace.json()["events"])
    assert context.client.get(f"/api/v1/agent-runs/{data['run_id']}/events", headers=r.other_headers).status_code == 404
    assert PRIVATE not in caplog.text


def test_worker_failure_is_safe_and_visible_in_durable_metrics(resources, workers, monkeypatch, caplog):
    r, context = resources, resources.context
    ready(r, workers)
    accepted = submit(context, r.own, r.headers, task=PRIVATE)
    assert accepted.status_code == 202, accepted.text
    data = accepted.json()

    def crash(*args, **kwargs):
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(context.app.state.adapter, "run_agent", crash)
    with pytest.raises(RuntimeError):
        workers.agent(data["task_id"])
    failures = [entry for entry in records(caplog, "task_failed") if entry["task_id"] == data["task_id"]]
    assert len(failures) == 1 and failures[0]["run_id"] == data["run_id"]
    assert failures[0]["error_code"] == "agent_run_failed"
    with context.uow_factory() as uow, uow.transaction():
        rows = uow.background_tasks.metrics_snapshot()
    assert next(row for row in rows if row["task_type"] == "agent_run" and row["status"] == "failed")["count"] == 1
    assert PRIVATE not in caplog.text


@pytest.mark.parametrize("value", ["short", "x" * 257, " " * 40, "汉" * 40, "x" * 39 + "\n"])
def test_metrics_token_config_is_bounded_and_never_reflects_secret(value):
    with pytest.raises(ValueError) as error:
        ObservabilitySettings(value)
    assert value not in str(error.value)
    assert METRICS_TOKEN not in repr(ObservabilitySettings(METRICS_TOKEN))
