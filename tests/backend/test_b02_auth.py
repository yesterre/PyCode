from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import logging
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import jwt
import pytest
from sqlalchemy import delete, update

from backend.app.auth import hash_password, verify_password
from backend.app.config import AuthSettings
from backend.app.core.models import UserStatus
from backend.app.db.models import UserORM
from backend.app.main import create_app
from backend.app.repositories import InMemoryUnitOfWork
from backend.app.services.auth import AuthService
from test_b01_users import uow_factory
from test_b02_security import JWT_SECRET, PASSWORD, claims_for
from test_postgresql import postgresql_engine


@pytest.fixture
def auth_context(uow_factory, application):
    with uow_factory() as uow:
        persistence = uow.state if isinstance(uow, InMemoryUnitOfWork) else None
        database_url = None if persistence is not None else uow.session.bind.url.render_as_string(hide_password=False)
    app = create_app(
        persistence=persistence, database_url=database_url, auth_settings=AuthSettings(JWT_SECRET),
        adapter=application.state.adapter, workspace=application.state.workspace,
        task_dispatcher=application.state.task_dispatcher,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield SimpleNamespace(client=client, uow_factory=uow_factory, app=app)


def register(context, account="alice", password=PASSWORD):
    return context.client.post("/api/v1/auth/register", json={"account_name": account, "password": password})


def login(context, account="alice", password=PASSWORD):
    return context.client.post("/api/v1/auth/login", json={"account_name": account, "password": password})


def seed_user(context, *, status=UserStatus.ACTIVE, encoded_hash=None, account="alice"):
    with context.uow_factory() as uow, uow.transaction():
        return uow.users.create(account, encoded_hash or hash_password(PASSWORD), status=status)


def assert_unauthenticated(response):
    assert response.status_code == 401, response.text
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {"detail": {"code": "unauthenticated", "message": "Authentication failed."}}


def test_register_login_and_current_user_use_existing_persistence(auth_context):
    context = auth_context
    with context.uow_factory() as uow, uow.transaction():
        legacy = uow.projects.create("legacy", "https://github.com/example/demo.git")
    created = register(context, " Alice ")
    assert created.status_code == 201, created.text
    user_id = UUID(created.json()["id"])
    assert created.json()["account_name"] == "alice"
    assert set(created.json()) == {"id", "account_name", "status", "created_at"}
    assert created.headers["Cache-Control"] == "no-store"
    with context.uow_factory() as uow, uow.transaction():
        user = uow.users.get(user_id)
        assert user.password_hash.startswith("$argon2id$")
        assert verify_password(PASSWORD, user.password_hash)
        assert uow.projects.get(legacy.id).owner_id is None
    authenticated = login(context, " ALICE ")
    assert authenticated.status_code == 200, authenticated.text
    assert authenticated.json()["token_type"] == "bearer"
    assert authenticated.json()["expires_in"] == 900
    assert authenticated.headers["Cache-Control"] == "no-store"
    token = authenticated.json()["access_token"]
    current = context.client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + token})
    assert current.status_code == 200 and current.json() == created.json()
    assert user.password_hash not in created.text + authenticated.text + current.text
    assert PASSWORD not in created.text + authenticated.text + current.text


def test_duplicate_registration_is_normalized_and_does_not_replace_password(auth_context):
    first = register(auth_context)
    assert first.status_code == 201
    duplicate = register(auth_context, " ALICE ", "a completely different password")
    assert duplicate.status_code == 409 and duplicate.json()["detail"]["code"] == "account_conflict"
    assert login(auth_context).status_code == 200
    assert_unauthenticated(login(auth_context, password="a completely different password"))


@pytest.mark.parametrize("body", [
    {}, {"account_name": "alice"}, {"account_name": "ab", "password": PASSWORD},
    {"account_name": "用户", "password": PASSWORD}, {"account_name": "../alice", "password": PASSWORD},
    {"account_name": "a" * 65, "password": PASSWORD},
    {"account_name": "alice", "password": "too-short"},
    {"account_name": "alice", "password": "x" * 129},
    {"account_name": "alice", "password": None},
    {"account_name": "alice", "password": {"private": PASSWORD}},
    {"account_name": "alice", "password": PASSWORD, "user_id": str(uuid4())},
    {"account_name": "alice", "password": PASSWORD, "status": "active"},
    {"account_name": "alice", "password": PASSWORD, "password_hash": "private-hash"},
])
def test_registration_validation_is_sanitized_and_has_no_writes(auth_context, body, caplog):
    with caplog.at_level(logging.ERROR):
        response = auth_context.client.post("/api/v1/auth/register", json=body)
    assert response.status_code == 422, response.text
    assert PASSWORD not in response.text + caplog.text and "private-hash" not in response.text + caplog.text
    assert all("input" not in error and "ctx" not in error for error in response.json()["detail"])
    with auth_context.uow_factory() as uow, uow.transaction():
        assert uow.users.get_by_account_name("alice") is None


def test_malformed_json_and_login_validation_do_not_reflect_credentials(auth_context, caplog):
    with caplog.at_level(logging.ERROR):
        malformed = auth_context.client.post(
            "/api/v1/auth/register", content='{"password":"private-json-password",',
            headers={"Content-Type": "application/json"},
        )
        invalid = auth_context.client.post("/api/v1/auth/login", json={"account_name": "alice", "password": PASSWORD, "token": "private-token"})
    assert malformed.status_code == invalid.status_code == 422
    assert "private-json-password" not in malformed.text + caplog.text
    assert PASSWORD not in invalid.text + caplog.text and "private-token" not in invalid.text + caplog.text


@pytest.mark.parametrize("mode", ["unknown", "wrong_password", "disabled", "legacy_hash"])
def test_login_failures_are_uniform(auth_context, mode):
    if mode != "unknown":
        seed_user(auth_context, status=UserStatus.DISABLED if mode == "disabled" else UserStatus.ACTIVE,
                  encoded_hash="legacy-opaque-hash" if mode == "legacy_hash" else None)
    response = login(auth_context, password="incorrect" if mode == "wrong_password" else PASSWORD)
    assert_unauthenticated(response)
    assert PASSWORD not in response.text


@pytest.mark.parametrize("authorization", [None, "Basic abc", "Bearer", "Bearer not-a-jwt", "Bearer " + "x" * 4097])
def test_missing_or_malformed_authorization_is_uniform(auth_context, authorization):
    headers = {} if authorization is None else {"Authorization": authorization}
    assert_unauthenticated(auth_context.client.get("/api/v1/auth/me", headers=headers))


@pytest.mark.parametrize("mode", ["expired", "future", "signature", "algorithm", "unsigned", "missing_exp", "unknown_user", "wrong_type"])
def test_invalid_tokens_cannot_authenticate(auth_context, mode):
    user = seed_user(auth_context)
    claims = claims_for(uuid4() if mode == "unknown_user" else user.id)
    key, algorithm = JWT_SECRET, "HS256"
    if mode == "expired":
        claims.update(iat=1, nbf=1, exp=2)
    elif mode == "future":
        claims.update(iat=9999999999, nbf=9999999999, exp=10000000899)
    elif mode == "signature":
        key = "a completely different test signing key" * 2
    elif mode == "algorithm":
        algorithm = "HS384"
    elif mode == "unsigned":
        key, algorithm = "", "none"
    elif mode == "missing_exp":
        claims.pop("exp")
    elif mode == "wrong_type":
        claims["token_type"] = "refresh"
    token = jwt.encode(claims, key, algorithm=algorithm)
    response = auth_context.client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + token})
    assert_unauthenticated(response)
    assert token not in response.text


@pytest.mark.parametrize("change", ["disable", "delete"])
def test_persisted_user_state_is_checked_on_every_request(auth_context, change):
    user = seed_user(auth_context)
    token = login(auth_context).json()["access_token"]
    with auth_context.uow_factory() as uow, uow.transaction():
        if isinstance(uow, InMemoryUnitOfWork):
            if change == "delete":
                del uow.state.users[user.id]
            else:
                uow.state.users[user.id] = replace(user, status=UserStatus.DISABLED)
        elif change == "delete":
            uow.session.execute(delete(UserORM).where(UserORM.id == user.id))
        else:
            uow.session.execute(update(UserORM).where(UserORM.id == user.id).values(status="disabled"))
    assert_unauthenticated(auth_context.client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + token}))


def test_query_token_and_untrusted_user_id_are_not_identity_sources(auth_context):
    user = seed_user(auth_context)
    token = login(auth_context).json()["access_token"]
    assert_unauthenticated(auth_context.client.get("/api/v1/auth/me", params={"access_token": token, "user_id": str(user.id)}))
    other = seed_user(auth_context, account="other-user")
    current = auth_context.client.get("/api/v1/auth/me", params={"user_id": str(other.id)}, headers={"Authorization": "Bearer " + token})
    assert current.status_code == 200 and current.json()["id"] == str(user.id)


def test_missing_secret_fails_closed_without_creating_users(auth_context, monkeypatch, caplog):
    auth_context.app.state.auth_settings = None
    monkeypatch.setenv("PYCODE_JWT_SECRET", "")
    with caplog.at_level(logging.ERROR):
        response = register(auth_context)
    assert response.status_code == 503 and response.json()["detail"]["code"] == "auth_unavailable"
    assert PASSWORD not in response.text + caplog.text
    with auth_context.uow_factory() as uow, uow.transaction():
        assert uow.users.get_by_account_name("alice") is None
    assert_unauthenticated(auth_context.client.get("/api/v1/auth/me"))
    assert auth_context.client.get("/health").status_code == 200


def test_auth_error_logs_do_not_include_exception_credentials(auth_context, monkeypatch, caplog):
    private = PASSWORD + JWT_SECRET + "private-encoded-hash private-access-token"

    def crash(*args, **kwargs):
        raise RuntimeError(private)

    monkeypatch.setattr(AuthService, "register", crash)
    with caplog.at_level(logging.ERROR, logger="pycode.backend"):
        response = register(auth_context)
    assert response.status_code == 500
    for value in (PASSWORD, JWT_SECRET, "private-encoded-hash", "private-access-token"):
        assert value not in response.text + caplog.text
    assert "internal_error" in caplog.text
    assert all(record.exc_info is None for record in caplog.records if record.name == "pycode.backend")


def test_concurrent_registration_has_one_winner(auth_context):
    barrier = Barrier(2)

    def create(account):
        barrier.wait(timeout=10)
        return register(auth_context, account).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(create, ["alice", " ALICE "])) == [201, 409]
    assert login(auth_context).status_code == 200


def test_swagger_declares_bearer_for_current_user_and_project_roots(auth_context):
    schema = auth_context.client.get("/openapi.json").json()
    assert schema["components"]["securitySchemes"]["BearerAuth"] == {
        "type": "http", "scheme": "bearer", "bearerFormat": "JWT",
    }
    assert schema["paths"]["/api/v1/auth/me"]["get"]["security"] == [{"BearerAuth": []}]
    assert not schema["paths"]["/api/v1/auth/register"]["post"].get("security")
    assert not schema["paths"]["/api/v1/auth/login"]["post"].get("security")
    for path, method in [
        ("/api/v1/projects", "get"), ("/api/v1/projects", "post"),
        ("/api/v1/projects/{project_id}", "get"),
    ]:
        assert schema["paths"][path][method]["security"] == [{"BearerAuth": []}]
        assert "401" in schema["paths"][path][method]["responses"]
