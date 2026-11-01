from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import socket
from threading import Event
from time import monotonic
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from redis import Redis
from sqlalchemy import text

from backend import check_config
from backend.app.config import AuthSettings, validate_deployment_configuration
from backend.app.db.session import DatabaseSessionManager
from backend.app.main import create_app
from backend.app.readiness import ReadinessProbe
from test_postgresql import _safe_test_database_url, postgresql_engine


PRIVATE = "b15-private-password-token-do-not-reflect"
AUTH = AuthSettings("b15-test-only-signing-key-" * 2)


@pytest.fixture
def deployment_env(monkeypatch):
    for line in (Path(__file__).resolve().parents[2] / ".env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test:test@localhost/pycode_test")
    monkeypatch.setenv("PYCODE_JWT_SECRET", AUTH.jwt_secret)


@pytest.mark.parametrize("name,value", [
    ("PYCODE_JWT_SECRET", PRIVATE[:8]), ("PYCODE_JWT_ALGORITHM", "none"),
    ("PYCODE_ACCESS_TOKEN_TTL_SECONDS", "0"), ("PYCODE_METRICS_TOKEN", "short"),
    ("DATABASE_URL", PRIVATE), ("DATABASE_URL", "postgresql+psycopg://u:replace-with-secret@localhost/db"),
    ("DATABASE_URL", "sqlite:///private.db"), ("REDIS_URL", PRIVATE),
    ("REDIS_URL", "redis://localhost:not-a-port/0"), ("REDIS_URL", "redis://localhost/0?socket_timeout=nan"),
    ("PYCODE_RATE_INDEX_REQUESTS", "0"), ("PYCODE_RATE_WINDOW_SECONDS", PRIVATE),
    ("PYCODE_MAX_ACTIVE_TASKS_PER_USER", "0"), ("PYCODE_REDIS_SOCKET_TIMEOUT_SECONDS", "nan"),
    ("PYCODE_REALTIME_STREAM_MAXLEN", "0"), ("PYCODE_TASK_LEASE_TTL_SECONDS", "330"),
    ("PYCODE_TASK_HEARTBEAT_INTERVAL_SECONDS", "420"), ("PYCODE_PROJECT_LOCK_RENEW_SECONDS", "300"),
    ("PYCODE_CELERY_VISIBILITY_TIMEOUT_SECONDS", "300"), ("PYCODE_QUEUED_RECONCILE_INTERVAL_SECONDS", "inf"),
    ("PYCODE_LLM_READ_TIMEOUT_SECONDS", "nan"), ("PYCODE_GIT_TIMEOUT_SECONDS", "inf"),
])
def test_preflight_rejects_invalid_configuration_without_reflecting_values(deployment_env, monkeypatch, capsys, name, value):
    monkeypatch.setenv(name, value)
    assert check_config.main(["--role", "api"]) == 1
    output = capsys.readouterr()
    assert "Invalid " in output.err
    assert PRIVATE not in output.out + output.err and AUTH.jwt_secret not in output.out + output.err


def test_api_worker_and_migration_validate_their_own_dependencies(deployment_env, monkeypatch, capsys):
    for role in ("api", "worker", "migration"):
        assert check_config.main(["--role", role]) == 0
    monkeypatch.setenv("PYCODE_JWT_SECRET", "")
    monkeypatch.setenv("PYCODE_JWT_ALGORITHM", "none")
    monkeypatch.setenv("PYCODE_METRICS_TOKEN", "bad")
    validate_deployment_configuration("worker")
    assert check_config.main(["--role", "api"]) == 1
    monkeypatch.setenv("REDIS_URL", "")
    assert check_config.main(["--role", "worker"]) == 1
    validate_deployment_configuration("migration")
    assert AUTH.jwt_secret not in str(capsys.readouterr())


@pytest.mark.parametrize("command,role", [
    (["python", "-m", "uvicorn", "backend.app.main:app"], "api"),
    (["python", "-m", "celery", "-A", "backend.app.worker:app", "worker"], "worker"),
    (["python", "-m", "celery", "-A", "backend.app.worker:app", "beat"], "worker"),
    (["python", "-m", "alembic", "upgrade", "head"], "migration"),
])
def test_docker_entrypoint_executes_existing_service_commands(deployment_env, monkeypatch, command, role):
    executed = []
    monkeypatch.setattr(check_config.os, "execvp", lambda name, args: executed.append((name, args)))
    assert check_config.command_role(command) == role
    assert check_config.main(["--exec", *command]) == 0
    assert executed == [(command[0], command)]


def test_invalid_entrypoint_does_not_execute_or_print_secret(deployment_env, monkeypatch, capsys):
    monkeypatch.setenv("PYCODE_JWT_SECRET", PRIVATE[:8])
    monkeypatch.setattr(check_config.os, "execvp", lambda *args: pytest.fail("Invalid configuration must not execute"))
    assert check_config.main(["--exec", "python", "-m", "uvicorn", "backend.app.main:app"]) == 1
    assert check_config.main(["--exec"]) == 1
    assert PRIVATE not in str(capsys.readouterr())


@pytest.fixture
def ready_app(postgresql_engine, monkeypatch):
    import os
    redis_url = os.environ.get("TEST_REDIS_URL") or os.environ.get("REDIS_URL")
    if not redis_url:
        pytest.skip("A real test Redis is required")
    monkeypatch.setenv("REDIS_URL", redis_url)
    client = Redis.from_url(redis_url, socket_connect_timeout=1, socket_timeout=1)
    client.ping()
    client.close()
    app = create_app(database_url=_safe_test_database_url(), auth_settings=AUTH)
    with TestClient(app) as client:
        yield app, client


def test_ready_checks_real_dependencies_and_keeps_health_contract(ready_app):
    app, client = ready_app
    assert client.get("/health").json() == {"status": "ok"}
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"configuration": True, "postgresql": True, "redis": True}}
    assert response.headers["cache-control"] == "no-store" and response.headers["x-request-id"]
    assert "/ready" not in client.get("/openapi.json").json()["paths"]


@pytest.mark.parametrize("state", ["missing", "stale"])
def test_ready_rejects_missing_or_old_schema_without_changing_business_data(ready_app, postgresql_engine, state):
    _, client = ready_app
    with postgresql_engine.begin() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        connection.execute(text("DELETE FROM alembic_version"))
        if state == "stale":
            connection.execute(text("INSERT INTO alembic_version(version_num) VALUES('b15-stale')"))
    try:
        response = client.get("/ready")
        assert response.status_code == 503 and response.json()["checks"]["postgresql"] is False
        assert client.get("/health").status_code == 200
    finally:
        with postgresql_engine.begin() as connection:
            connection.execute(text("DELETE FROM alembic_version"))
            connection.execute(text("INSERT INTO alembic_version(version_num) VALUES(:revision)"), {"revision": revision})


def test_readiness_statement_timeout_is_local_and_bounded(ready_app, postgresql_engine):
    app, client = ready_app
    with app.state.database.open_session() as session:
        original = (session.execute(text("SHOW statement_timeout")).scalar_one(),
                    session.execute(text("SHOW default_transaction_read_only")).scalar_one())
    with postgresql_engine.begin() as connection:
        connection.execute(text("LOCK TABLE alembic_version IN ACCESS EXCLUSIVE MODE"))
        started = monotonic()
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json()["checks"]["postgresql"] is False
        assert .8 < monotonic() - started < 4
        assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").status_code == 200
    with app.state.database.open_session() as session:
        assert (session.execute(text("SHOW statement_timeout")).scalar_one(),
                session.execute(text("SHOW default_transaction_read_only")).scalar_one()) == original


def test_ready_does_not_wait_for_the_exhausted_business_pool(ready_app):
    app, client = ready_app
    with app.state.database.open_session() as session:
        session.execute(text("SELECT 1"))
    pool = app.state.database._engine
    held = [pool.connect() for _ in range(15)]
    try:
        started = monotonic()
        assert client.get("/ready").status_code == 200
        assert monotonic() - started < 4
    finally:
        for connection in held:
            connection.close()


def test_dependency_outages_and_bad_auth_do_not_change_liveness(ready_app, monkeypatch, caplog):
    app, client = ready_app
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        app.state.readiness.database = DatabaseSessionManager(f"postgresql+psycopg://u:{PRIVATE}@127.0.0.1:{port}/db")
        monkeypatch.setenv("REDIS_URL", f"redis://u:{PRIVATE}@127.0.0.1:{port}/0")
        app.state.readiness.close()
        app.state.readiness.client = None
        app.state.auth_settings = None
        monkeypatch.setenv("PYCODE_JWT_SECRET", "")
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["checks"] == {"configuration": False, "postgresql": False, "redis": False}
    assert client.get("/health").json() == {"status": "ok"}
    assert PRIVATE not in response.text + caplog.text


def test_readiness_is_single_flight_and_redis_probe_options_are_bounded(monkeypatch):
    entered, release = Event(), Event()

    def blocked():
        entered.set()
        assert release.wait(5)
        return True

    probe = ReadinessProbe(SimpleNamespace(probe=blocked))
    probe.client = SimpleNamespace(ping=lambda: True)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(probe.check, AUTH)
        assert entered.wait(3)
        assert probe.check(AUTH) == {"probe": False}
        release.set()
        assert all(first.result().values())
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        monkeypatch.setenv("REDIS_URL", f"redis://127.0.0.1:{reserved.getsockname()[1]}/0?socket_timeout=30&socket_connect_timeout=30&max_connections=999")
        probe = ReadinessProbe(None)
        assert probe.check(AUTH)["redis"] is False
        assert probe.client.connection_pool.max_connections == 1
        assert probe.client.connection_pool.connection_kwargs["socket_timeout"] == 1
        assert probe.client.connection_pool.connection_kwargs["socket_connect_timeout"] == 1
        probe.close()
