from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID, uuid4

import jwt
import pytest
from sqlalchemy import update

from backend.app.core.errors import BackendError
from backend.app.core.models import UserStatus
from backend.app.db.models import ProjectORM, UserORM
from backend.app.repositories import InMemoryUnitOfWork
from backend.app.runtime import build_project_service
from backend.app.services.projects import ProjectService
from test_b01_users import uow_factory
from test_b02_auth import auth_context, login, register
from test_b02_security import JWT_SECRET, claims_for
from test_postgresql import postgresql_engine


ROOT = "/api/v1/projects"
BODY = {"name": "Example", "repo_url": "https://github.com/example/demo.git"}


def assert_unauthenticated(response):
    assert response.status_code == 401, response.text
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json() == {"detail": {"code": "unauthenticated", "message": "Authentication failed."}}


def actor(context, account):
    registered = register(context, account)
    assert registered.status_code == 201, registered.text
    authenticated = login(context, account)
    assert authenticated.status_code == 200, authenticated.text
    return UUID(registered.json()["id"]), {"Authorization": "Bearer " + authenticated.json()["access_token"]}


def create(context, headers, **kwargs):
    response = context.client.post(ROOT, headers=headers, json=BODY, **kwargs)
    assert response.status_code == 201, response.text
    return response.json()


def project_ids(response):
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["projects"]]


def test_two_users_own_creation_and_cannot_read_each_others_projects(auth_context):
    context = auth_context
    # Pre-registration legacy data must never be assigned to the first account.
    with context.uow_factory() as uow, uow.transaction():
        legacy = uow.projects.create("legacy", BODY["repo_url"])
    alice, a = actor(context, "alice")
    bob, b = actor(context, "bob")
    first, second = create(context, a), create(context, b)
    assert first["id"] != second["id"]
    assert "owner_id" not in first  # Preserve the public Project response contract.
    for own, headers, other in [(first, a, second), (second, b, first)]:
        assert project_ids(context.client.get(ROOT, headers=headers)) == [own["id"]]
        detail = context.client.get(ROOT + "/" + own["id"], headers=headers)
        assert detail.status_code == 200, detail.text
        # PostgreSQL may render the same instant in its configured session timezone.
        expected, actual = dict(own), detail.json()
        for field in ("created_at", "updated_at"):
            expected[field] = datetime.fromisoformat(expected[field])
            actual[field] = datetime.fromisoformat(actual[field])
        assert actual == expected
        denied = [context.client.get(ROOT + "/" + identifier, headers=headers)
                  for identifier in (other["id"], str(legacy.id), str(uuid4()))]
        assert all(response.status_code == 404 for response in denied)
        assert all(response.json() == {"detail": {
            "code": "project_not_found", "message": "Project does not exist.",
        }} for response in denied)
    with context.uow_factory() as uow, uow.transaction():
        assert uow.projects.get(UUID(first["id"])).owner_id == alice
        assert uow.projects.get(UUID(second["id"])).owner_id == bob
        assert uow.projects.get(legacy.id).owner_id is None
    assert context.app.state.workspace.source.calls == []
    assert context.app.state.task_dispatcher.repository_index_task_ids == []


@pytest.mark.parametrize("field", ["owner_id", "user_id", "owner"])
def test_create_rejects_client_owner_without_writes(auth_context, field):
    _, headers = actor(auth_context, "alice")
    response = auth_context.client.post(ROOT, headers=headers, json={**BODY, field: str(uuid4())})
    assert response.status_code == 422, response.text
    with auth_context.uow_factory() as uow, uow.transaction():
        assert uow.projects.list_page(100, 0) == []


def test_query_identity_cannot_override_server_user(auth_context):
    alice, a = actor(auth_context, "alice")
    bob, b = actor(auth_context, "bob")
    other = create(auth_context, b)
    own = create(auth_context, a, params={"owner_id": str(bob), "user_id": str(bob)})
    assert project_ids(auth_context.client.get(ROOT, headers=a, params={"user_id": str(bob)})) == [own["id"]]
    assert auth_context.client.get(ROOT + "/" + other["id"], headers=a,
                                  params={"owner_id": str(bob)}).status_code == 404
    with auth_context.uow_factory() as uow, uow.transaction():
        assert uow.projects.get(UUID(own["id"])).owner_id == alice
    token = a["Authorization"].removeprefix("Bearer ")
    assert_unauthenticated(auth_context.client.get(ROOT, params={"access_token": token, "user_id": str(alice)}))


@pytest.mark.parametrize("mode", ["missing", "malformed", "signature", "expired", "unknown_user", "disabled"])
def test_every_root_operation_requires_verified_active_identity(auth_context, mode):
    context = auth_context
    user_id, headers = actor(context, "alice")
    own = create(context, headers)
    if mode == "missing":
        headers = {}
    elif mode == "malformed":
        headers = {"Authorization": "Bearer not-a-jwt"}
    elif mode == "disabled":
        with context.uow_factory() as uow, uow.transaction():
            if isinstance(uow, InMemoryUnitOfWork):
                uow.state.users[user_id] = replace(uow.users.get(user_id), status=UserStatus.DISABLED)
            else:
                uow.session.execute(update(UserORM).where(UserORM.id == user_id).values(status="disabled"))
    else:
        claims = claims_for(uuid4() if mode == "unknown_user" else user_id)
        if mode == "expired":
            claims.update(iat=1, nbf=1, exp=2)
        token = jwt.encode(claims, "wrong-test-signing-key-" * 3 if mode == "signature" else JWT_SECRET,
                           algorithm="HS256")
        headers = {"Authorization": "Bearer " + token}
    for method, url, kwargs in [
        ("get", ROOT, {}), ("post", ROOT, {"json": BODY}),
        ("get", ROOT + "/" + own["id"], {}),
    ]:
        assert_unauthenticated(context.client.request(method, url, headers=headers, **kwargs))
    with context.uow_factory() as uow, uow.transaction():
        assert len(uow.projects.list_page(100, 0)) == 1
    assert context.app.state.workspace.source.calls == []
    assert context.app.state.task_dispatcher.repository_index_task_ids == []


def test_owner_filter_precedes_pagination_and_ties_have_stable_order(auth_context):
    context = auth_context
    alice, a = actor(context, "alice")
    bob, b = actor(context, "bob")
    same_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with context.uow_factory() as uow, uow.transaction():
        owned = []
        for owner in [bob, alice, None, bob, alice, None, bob, alice, bob]:
            project = uow.projects.create("page", BODY["repo_url"], owner_id=owner)
            if owner == alice:
                owned.append(project.id)
            if isinstance(uow, InMemoryUnitOfWork):
                uow.state.projects[project.id] = replace(project, updated_at=same_time)
            else:
                uow.session.execute(update(ProjectORM).where(ProjectORM.id == project.id).values(updated_at=same_time))
        expected = [str(identifier) for identifier in sorted(owned, reverse=True)]
        assert [str(p.id) for p in uow.projects.list_owned_page(alice, 2, 1)] == expected[1:]
    seen = []
    for offset in range(4):
        response = context.client.get(ROOT, headers=a, params={"limit": 1, "offset": offset})
        seen.extend(project_ids(response))
        assert response.json()["has_more"] is (offset < 2)
    assert seen == expected
    assert len(project_ids(context.client.get(ROOT, headers=b))) == 4
    assert project_ids(context.client.get(ROOT, headers=a, params={"offset": 100})) == []


def test_foreign_and_legacy_projects_are_denied_before_summary_reads(auth_context, monkeypatch):
    alice, a = actor(auth_context, "alice")
    bob, _ = actor(auth_context, "bob")
    with auth_context.uow_factory() as uow, uow.transaction():
        foreign = uow.projects.create("foreign", BODY["repo_url"], owner_id=bob)
        legacy = uow.projects.create("legacy", BODY["repo_url"])

    def unexpected_summary(*args):
        raise AssertionError("Authorization must precede derived metadata reads")

    monkeypatch.setattr(ProjectService, "_with_summary", unexpected_summary)
    assert project_ids(auth_context.client.get(ROOT, headers=a)) == []
    for project_id in [foreign.id, legacy.id, uuid4()]:
        assert auth_context.client.get(ROOT + "/" + str(project_id), headers=a).status_code == 404
    with auth_context.uow_factory() as uow, uow.transaction():
        for project_id in [foreign.id, legacy.id, uuid4()]:
            with pytest.raises(BackendError, match="Project does not exist"):
                uow.projects.get_owned(project_id, alice)
        with pytest.raises(BackendError, match="Project does not exist"):
            uow.projects.get_owned(legacy.id, None)
        assert uow.projects.list_owned_page(None, 100, 0) == []


def test_owned_create_failure_rolls_back_and_allows_retry(auth_context):
    owner, headers = actor(auth_context, "alice")
    with auth_context.uow_factory() as uow:
        service = build_project_service(
            uow, adapter=auth_context.app.state.adapter, workspace=auth_context.app.state.workspace,
            artifacts=auth_context.app.state.artifacts, operations=auth_context.app.state.project_operations,
        )
        for invalid_owner, code in [(None, "unauthenticated"), (uuid4(), "database_conflict")]:
            with pytest.raises(BackendError) as failure:
                service.create_owned(invalid_owner, BODY["name"], BODY["repo_url"])
            assert failure.value.code == code
        with uow.transaction():
            assert uow.projects.list_page(100, 0) == []
    invalid_source = auth_context.client.post(ROOT, headers=headers, json={**BODY, "repo_url": "file:///private"})
    assert invalid_source.status_code == 422
    with auth_context.uow_factory() as uow, uow.transaction():
        assert uow.projects.list_owned_page(owner, 100, 0) == []
    assert create(auth_context, headers)["status"] == "created"
