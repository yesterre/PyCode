from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from backend.app.config import load_environment
from backend.app.repositories import SqlAlchemyUnitOfWork
from test_postgresql import _safe_test_database_url, _truncate


PREVIOUS = "20260915_0007"
REVISION = "20261029_0008"
PASSWORD_HASH = "scrypt$16384$8$1$test-salt$test-encoded-digest"


@pytest.fixture
def migration_database():
    load_environment()
    database_url = _safe_test_database_url()
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")
    command.downgrade(config, PREVIOUS)
    engine = create_engine(database_url, poolclass=NullPool, hide_parameters=True)
    try:
        yield config, engine
    finally:
        command.upgrade(config, "head")
        _truncate(engine)
        engine.dispose()


def test_empty_database_upgrades_and_can_downgrade_without_identity_data(migration_database):
    config, engine = migration_database
    command.downgrade(config, "base")
    assert "projects" not in inspect(engine).get_table_names()
    command.upgrade(config, REVISION)
    schema = inspect(engine)
    assert "users" in schema.get_table_names()
    assert {column["name"] for column in schema.get_columns("users")} == {
        "id", "account_name", "password_hash", "created_at", "status",
    }
    owner_column = next(column for column in schema.get_columns("projects") if column["name"] == "owner_id")
    assert owner_column["nullable"] is True and owner_column["default"] is None
    foreign_key = next(fk for fk in schema.get_foreign_keys("projects") if fk["constrained_columns"] == ["owner_id"])
    assert foreign_key["referred_table"] == "users"
    assert any(index["name"] == "ix_projects_owner_id" for index in schema.get_indexes("projects"))
    assert any(unique["column_names"] == ["account_name"] for unique in schema.get_unique_constraints("users"))
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM users")).scalar_one() == 0
    command.downgrade(config, PREVIOUS)
    assert "users" not in inspect(engine).get_table_names()
    assert "owner_id" not in {column["name"] for column in inspect(engine).get_columns("projects")}
    command.upgrade(config, REVISION)


def _legacy_records(engine):
    ids = {key: uuid4() for key in ("project", "snapshot", "run", "trace", "task", "token")}
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO projects (id,name,repo_url,status,workspace_path,created_at,updated_at) "
            "VALUES (:project,'legacy','https://github.com/example/demo.git','ready','workspace',now(),now())"
        ), ids)
        connection.execute(text(
            "INSERT INTO project_snapshots (id,project_id,commit_sha,index_artifact_path,"
            "graph_artifact_path,file_count,node_count,edge_count,status,created_at,updated_at) "
            "VALUES (:snapshot,:project,:commit,'index.json','graph.json',1,2,1,'ready',now(),now())"
        ), {**ids, "commit": "a" * 40})
        connection.execute(text("UPDATE projects SET current_snapshot_id=:snapshot WHERE id=:project"), ids)
        connection.execute(text(
            "INSERT INTO agent_runs (id,project_id,snapshot_id,task,run_type,status,"
            "created_at,started_at,core_started_at) "
            "VALUES (:run,:project,:snapshot,'legacy task','agent','running',now(),now(),now())"
        ), ids)
        connection.execute(text(
            "INSERT INTO trace_events (id,run_id,sequence,event_type,payload,created_at) "
            "VALUES (:trace,:run,0,'legacy_event','{}'::jsonb,now())"
        ), ids)
        connection.execute(text(
            "INSERT INTO background_tasks (id,project_id,snapshot_id,agent_run_id,"
            "task_type,status,attempt_count,idempotency_key,request_fingerprint,execution_token,"
            "lease_expires_at,last_heartbeat_at,worker_id,created_at,started_at) "
            "VALUES (:task,:project,:snapshot,:run,'agent_run','running',1,'legacy-key',"
            ":fingerprint,:token,now()+interval '10 minutes',now(),'legacy-worker',now(),now())"
        ), {**ids, "fingerprint": "f" * 64})
    return ids


def _records(engine):
    with engine.connect() as connection:
        return {
            table: [dict(row) for row in connection.execute(text(f"SELECT * FROM {table} ORDER BY id")).mappings()]
            for table in ("projects", "project_snapshots", "agent_runs", "trace_events", "background_tasks")
        }


def test_legacy_upgrade_preserves_all_records_and_never_assigns_first_user(migration_database):
    config, engine = migration_database
    ids = _legacy_records(engine)
    before = _records(engine)
    command.upgrade(config, REVISION)
    after = _records(engine)
    assert all(project.pop("owner_id") is None for project in after["projects"])
    assert after == before
    with Session(engine) as session:
        uow = SqlAlchemyUnitOfWork(session)
        with uow.transaction():
            user = uow.users.create("first-user", PASSWORD_HASH)
            # Test the B01 schema itself; current Project ORM also requires B08.
            assert session.execute(text("SELECT owner_id FROM projects WHERE id=:id"),
                                   {"id": ids["project"]}).scalar_one() is None
        with session.begin():
            # NULL ownership cannot match a user's owner predicate.
            assert session.execute(text(
                "SELECT id FROM projects WHERE owner_id=:owner"
            ), {"owner": user.id}).all() == []
        with session.begin():
            session.execute(text("DELETE FROM users WHERE id=:id"), {"id": user.id})
    command.downgrade(config, PREVIOUS)
    assert _records(engine) == before


def test_database_constraints_reject_noncanonical_users_duplicates_and_invalid_owners(migration_database):
    config, engine = migration_database
    command.upgrade(config, REVISION)
    owner = uuid4()
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO users (id,account_name,password_hash) VALUES (:id,'alice',:hash)"
        ), {"id": owner, "hash": PASSWORD_HASH})
    invalid = [
        {"account": "alice", "hash": PASSWORD_HASH, "status": "active"},
        {"account": "ALICE", "hash": PASSWORD_HASH, "status": "active"},
        {"account": " alice ", "hash": PASSWORD_HASH, "status": "active"},
        {"account": "ab", "hash": PASSWORD_HASH, "status": "active"},
        {"account": "用户", "hash": PASSWORD_HASH, "status": "active"},
        {"account": "bob", "hash": "", "status": "active"},
        {"account": "bob", "hash": "hash value", "status": "active"},
        {"account": "bob", "hash": PASSWORD_HASH, "status": "administrator"},
    ]
    for values in invalid:
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO users (id,account_name,password_hash,status) VALUES (:id,:account,:hash,:status)"
            ), {**values, "id": uuid4()})
    ids = _legacy_records(engine)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(text("UPDATE projects SET owner_id=:owner WHERE id=:project"),
                           {"owner": uuid4(), "project": ids["project"]})
    with engine.begin() as connection:
        connection.execute(text("UPDATE projects SET owner_id=:owner WHERE id=:project"),
                           {"owner": owner, "project": ids["project"]})
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(text("DELETE FROM users WHERE id=:id"), {"id": owner})


@pytest.mark.parametrize("assign_owner", [False, True])
def test_downgrade_refuses_to_erase_user_or_ownership_data(migration_database, assign_owner):
    config, engine = migration_database
    ids = _legacy_records(engine)
    command.upgrade(config, REVISION)
    with Session(engine) as session:
        uow = SqlAlchemyUnitOfWork(session)
        with uow.transaction():
            user = uow.users.create("alice", PASSWORD_HASH)
            if assign_owner:
                # Keep this historical downgrade test independent of later columns.
                session.execute(text("UPDATE projects SET owner_id=:owner WHERE id=:id"),
                                {"owner": user.id, "id": ids["project"]})
    before = _records(engine)
    with pytest.raises(RuntimeError, match="Refusing to remove"):
        command.downgrade(config, PREVIOUS)
    assert _records(engine) == before
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == REVISION
        assert connection.execute(text("SELECT id FROM users")).scalar_one() == user.id
