from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.app.core.errors import BackendError
from backend.app.core.models import IndexSummary, ProjectStatus, UserStatus
from backend.app.repositories import InMemoryPersistence, InMemoryUnitOfWork, SqlAlchemyUnitOfWork
from backend.manage_project_owner import assign_project_owner, main
from test_postgresql import _truncate, postgresql_engine  # Reuse the guarded real DB fixture.


PASSWORD_HASH = "scrypt$16384$8$1$test-salt$test-encoded-digest"
REPO_URL = "https://github.com/example/demo.git"


@pytest.fixture(params=["memory", "postgresql"])
def uow_factory(request):
    state = InMemoryPersistence()
    engine = request.getfixturevalue("postgresql_engine") if request.param == "postgresql" else None

    def clean():
        if engine is not None:
            _truncate(engine)

    @contextmanager
    def factory():
        if engine is None:
            yield InMemoryUnitOfWork(state)
        else:
            with Session(engine, autoflush=False, expire_on_commit=False) as session:
                yield SqlAlchemyUnitOfWork(session)

    clean()
    try:
        yield factory
    finally:
        clean()


def test_user_round_trip_normalizes_account_and_keeps_hash_out_of_repr(uow_factory):
    with uow_factory() as uow, uow.transaction():
        user = uow.users.create(" Alice.Example ", PASSWORD_HASH)
        disabled = uow.users.create("disabled-user", PASSWORD_HASH, status=UserStatus.DISABLED)
        assert user.account_name == "alice.example"
        assert user.status == UserStatus.ACTIVE
        assert disabled.status == UserStatus.DISABLED
        assert PASSWORD_HASH not in repr(user)
        assert user.created_at.utcoffset().total_seconds() == 0
    with uow_factory() as uow, uow.transaction():
        assert uow.users.get(user.id) == user
        assert uow.users.get_by_account_name(" ALICE.Example ") == user
        assert uow.users.get_by_account_name("unknown") is None


@pytest.mark.parametrize("account", ["", "ab", "a" * 65, "a b", "../alice", "_alice", "用户", "Kelvin", None])
def test_invalid_account_is_rejected_without_persisting(uow_factory, account):
    with uow_factory() as uow:
        with pytest.raises(BackendError) as failure, uow.transaction():
            uow.users.create(account, PASSWORD_HASH)
        assert failure.value.code == "invalid_user"
        assert PASSWORD_HASH not in str(failure.value)


@pytest.mark.parametrize("password_hash", ["", " ", "hash\nvalue", "x" * 1025, None])
def test_invalid_encoded_hash_is_rejected(uow_factory, password_hash):
    with uow_factory() as uow:
        with pytest.raises(BackendError) as failure, uow.transaction():
            uow.users.create("alice", password_hash)
        assert failure.value.code == "invalid_user"


def test_unknown_status_and_invalid_lookup_have_safe_errors(uow_factory):
    with uow_factory() as uow:
        with pytest.raises(BackendError) as failure, uow.transaction():
            uow.users.create("alice", PASSWORD_HASH, status="administrator")
        assert failure.value.code == "invalid_user"
        with pytest.raises(BackendError) as failure, uow.transaction():
            uow.users.get(uuid4())
        assert failure.value.code == "user_not_found"
        with pytest.raises(BackendError) as failure, uow.transaction():
            uow.users.get_by_account_name("../invalid")
        assert failure.value.code == "invalid_user"


def test_duplicate_normalized_account_rolls_back_whole_transaction(uow_factory):
    with uow_factory() as uow, uow.transaction():
        existing = uow.users.create("alice", PASSWORD_HASH)
    with uow_factory() as uow:
        with pytest.raises(BackendError) as failure, uow.transaction():
            uow.users.create("bob", PASSWORD_HASH)
            uow.projects.create("rolled back", REPO_URL)
            uow.users.create(" ALICE ", PASSWORD_HASH)
        assert failure.value.code == "database_conflict"
        assert PASSWORD_HASH not in str(failure.value)
        with uow.transaction():
            assert uow.users.get_by_account_name("bob") is None
            assert uow.users.get(existing.id) == existing
            assert uow.projects.list_page(10, 0) == []


def test_creating_users_never_claims_legacy_projects(uow_factory):
    with uow_factory() as uow, uow.transaction():
        legacy = uow.projects.create("legacy", REPO_URL)
        alice = uow.users.create("alice", PASSWORD_HASH)
        uow.users.create("bob", PASSWORD_HASH)
        owned = uow.projects.create("owned", REPO_URL, owner_id=alice.id)
    with uow_factory() as uow, uow.transaction():
        assert uow.projects.get(legacy.id).owner_id is None
        assert uow.projects.get(owned.id).owner_id == alice.id
        assert legacy.owner_id not in {alice.id}


def test_invalid_project_owner_is_a_conflict_and_rolls_back(uow_factory):
    with uow_factory() as uow:
        with pytest.raises(BackendError) as failure, uow.transaction():
            uow.projects.create("invalid", REPO_URL, owner_id=uuid4())
        assert failure.value.code == "database_conflict"
        with uow.transaction():
            assert uow.projects.list_page(10, 0) == []


def test_owner_survives_existing_project_and_snapshot_updates(uow_factory):
    with uow_factory() as uow, uow.transaction():
        owner = uow.users.create("alice", PASSWORD_HASH)
        project = uow.projects.create("owned", REPO_URL, owner_id=owner.id)
        snapshot = uow.snapshots.upsert(
            project.id, "a" * 40, index_artifact_path="index.json",
            graph_artifact_path="graph.json", summary=IndexSummary(1, 2, 1),
        )
        assert uow.projects.set_status(project.id, ProjectStatus.INDEXING).owner_id == owner.id
        assert uow.projects.set_current_snapshot(project.id, snapshot.id).owner_id == owner.id
        assert uow.projects.set_status(project.id, ProjectStatus.READY).owner_id == owner.id
    with uow_factory() as uow, uow.transaction():
        stored = uow.projects.get(project.id)
        assert stored.owner_id == owner.id and stored.current_snapshot_id == snapshot.id


def test_legacy_assignment_requires_explicit_apply_and_cannot_reassign(uow_factory):
    with uow_factory() as uow, uow.transaction():
        legacy = uow.projects.create("legacy", REPO_URL)
        alice = uow.users.create("alice", PASSWORD_HASH)
        bob = uow.users.create("bob", PASSWORD_HASH)
    with uow_factory() as uow:
        assign_project_owner(uow, legacy.id, alice.id)
        with uow.transaction():
            assert uow.projects.get(legacy.id).owner_id is None
        assign_project_owner(uow, legacy.id, alice.id, apply=True)
        with pytest.raises(BackendError) as failure:
            assign_project_owner(uow, legacy.id, bob.id, apply=True)
        assert failure.value.code == "database_conflict"
    with uow_factory() as uow, uow.transaction():
        assert uow.projects.get(legacy.id).owner_id == alice.id


@pytest.mark.parametrize("apply", [False, True])
def test_disabled_or_missing_user_cannot_claim_legacy_project(uow_factory, apply):
    with uow_factory() as uow, uow.transaction():
        legacy = uow.projects.create("legacy", REPO_URL)
        disabled = uow.users.create("disabled", PASSWORD_HASH, status=UserStatus.DISABLED)
    with uow_factory() as uow:
        for owner_id, expected in [(disabled.id, "database_conflict"), (uuid4(), "user_not_found")]:
            with pytest.raises(BackendError) as failure:
                assign_project_owner(uow, legacy.id, owner_id, apply=apply)
            assert failure.value.code == expected
        with pytest.raises(BackendError) as failure:
            assign_project_owner(uow, uuid4(), disabled.id, apply=apply)
        assert failure.value.code == "project_not_found"
        with uow.transaction():
            assert uow.projects.get(legacy.id).owner_id is None


def test_assignment_is_rolled_back_with_other_changes(uow_factory):
    with uow_factory() as uow, uow.transaction():
        legacy = uow.projects.create("legacy", REPO_URL)
        owner = uow.users.create("alice", PASSWORD_HASH)
    with uow_factory() as uow:
        with pytest.raises(RuntimeError), uow.transaction():
            uow.projects.assign_legacy_owner(legacy.id, owner.id)
            raise RuntimeError("abort maintenance transaction")
        with uow.transaction():
            assert uow.projects.get(legacy.id).owner_id is None


def test_concurrent_duplicate_accounts_have_one_winner(uow_factory):
    barrier = Barrier(2)

    def create(name):
        barrier.wait(timeout=10)
        try:
            with uow_factory() as uow, uow.transaction():
                uow.users.create(name, PASSWORD_HASH)
            return "created"
        except BackendError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(create, ["alice", " ALICE "])) == ["created", "database_conflict"]


def test_concurrent_legacy_claims_cannot_overwrite_each_other(uow_factory):
    with uow_factory() as uow, uow.transaction():
        legacy = uow.projects.create("legacy", REPO_URL)
        owners = [uow.users.create(name, PASSWORD_HASH).id for name in ("alice", "bob")]
    barrier = Barrier(2)

    def assign(owner_id):
        barrier.wait(timeout=10)
        try:
            with uow_factory() as uow, uow.transaction():
                uow.projects.assign_legacy_owner(legacy.id, owner_id)
            return "assigned", owner_id
        except BackendError as exc:
            return exc.code, owner_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(assign, owners))
    assert sorted(result[0] for result in results) == ["assigned", "database_conflict"]
    winner = next(owner_id for result, owner_id in results if result == "assigned")
    with uow_factory() as uow, uow.transaction():
        assert uow.projects.get(legacy.id).owner_id == winner


def test_cli_dry_run_apply_and_rejection_use_real_database(postgresql_engine, monkeypatch, capsys):
    engine = postgresql_engine
    monkeypatch.setenv("DATABASE_URL", engine.url.render_as_string(hide_password=False))
    with Session(engine) as session:
        uow = SqlAlchemyUnitOfWork(session)
        with uow.transaction():
            legacy = uow.projects.create("cli legacy", REPO_URL)
            owner = uow.users.create("cli-user", PASSWORD_HASH)
    args = ["--project-id", str(legacy.id), "--owner-id", str(owner.id)]
    try:
        assert main(args) == 0
        with Session(engine) as session:
            assert SqlAlchemyUnitOfWork(session).projects.get(legacy.id).owner_id is None
        assert main([*args, "--apply"]) == 0
        with Session(engine) as session:
            assert SqlAlchemyUnitOfWork(session).projects.get(legacy.id).owner_id == owner.id
        assert main([*args, "--apply"]) == 1
        output = capsys.readouterr().out
        assert "Dry-run only" in output and "Applied" in output and "database_conflict" in output
        assert PASSWORD_HASH not in output
    finally:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM projects WHERE id=:id"), {"id": legacy.id})
            connection.execute(text("DELETE FROM users WHERE id=:id"), {"id": owner.id})


def test_cli_requires_explicit_valid_identifiers():
    with pytest.raises(SystemExit) as error:
        main(["--project-id", "invalid", "--owner-id", str(uuid4())])
    assert error.value.code == 2
    with pytest.raises(SystemExit) as error:
        main([])
    assert error.value.code == 2


def test_cli_database_configuration_failure_does_not_expose_credentials(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql+nonexistent://user:private-password@localhost/db")
    assert main(["--project-id", str(uuid4()), "--owner-id", str(uuid4())]) == 1
    output = capsys.readouterr()
    assert output.out.strip() == "Ownership assignment rejected: database_unavailable"
    assert "private-password" not in output.out + output.err
