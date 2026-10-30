from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from test_b01_migration import PASSWORD_HASH, _legacy_records, _records
from test_postgresql import _safe_test_database_url, _truncate, postgresql_engine


PREVIOUS = "20261029_0008"
REVISION = "20261030_0009"


@pytest.fixture
def archive_database(postgresql_engine):
    _safe_test_database_url()
    engine = postgresql_engine
    _truncate(engine)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["database_url"] = _safe_test_database_url()
    command.downgrade(config, PREVIOUS)
    try:
        yield config, engine
    finally:
        command.upgrade(config, "head")
        _truncate(engine)


def test_archive_upgrade_preserves_owners_legacy_data_tasks_snapshots_and_trace(archive_database):
    config, engine = archive_database
    ids = _legacy_records(engine)
    owner = uuid4()
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO users (id,account_name,password_hash) VALUES (:id,'archive-owner',:hash)"
        ), {"id": owner, "hash": PASSWORD_HASH})
        connection.execute(text("UPDATE projects SET owner_id=:owner WHERE id=:id"),
                           {"owner": owner, "id": ids["project"]})
    before = _records(engine)
    command.upgrade(config, REVISION)
    column = next(c for c in inspect(engine).get_columns("projects") if c["name"] == "archived_at")
    assert column["nullable"] and column["type"].timezone
    assert column["default"] is None
    after = _records(engine)
    assert all(project.pop("archived_at") is None for project in after["projects"])
    assert after == before
    command.downgrade(config, PREVIOUS)
    assert _records(engine) == before
    assert "archived_at" not in {c["name"] for c in inspect(engine).get_columns("projects")}
    command.upgrade(config, REVISION)
    assert all(project["archived_at"] is None for project in _records(engine)["projects"])


def test_downgrade_refuses_to_silently_unarchive_existing_projects(archive_database):
    config, engine = archive_database
    ids = _legacy_records(engine)
    command.upgrade(config, REVISION)
    with engine.begin() as connection:
        connection.execute(text("UPDATE projects SET archived_at=now() WHERE id=:id"),
                           {"id": ids["project"]})
    before = _records(engine)
    with pytest.raises(RuntimeError, match="Refusing to remove Project archive data"):
        command.downgrade(config, PREVIOUS)
    assert _records(engine) == before
    with engine.begin() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == REVISION
        connection.execute(text("UPDATE projects SET archived_at=NULL WHERE id=:id"),
                           {"id": ids["project"]})
    command.downgrade(config, PREVIOUS)
    after = _records(engine)
    for project in before["projects"]:
        project.pop("archived_at")
    assert after == before
