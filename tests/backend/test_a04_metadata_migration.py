from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect,text
from sqlalchemy.exc import IntegrityError

from test_b01_migration import _legacy_records,_records
from test_postgresql import _safe_test_database_url,_truncate,postgresql_engine

PREVIOUS='20261030_0009'
REVISION='20261104_0010'
FIELDS=('request_metadata','execution_metadata','result_metadata')


@pytest.fixture
def metadata_database(postgresql_engine):
    _safe_test_database_url();engine=postgresql_engine;_truncate(engine)
    config=Config(str(Path(__file__).resolve().parents[2]/'alembic.ini'))
    config.attributes['database_url']=_safe_test_database_url()
    command.downgrade(config,PREVIOUS)
    try:yield config,engine
    finally:
        command.upgrade(config,'head');_truncate(engine)


def test_upgrade_preserves_all_legacy_rows_and_empty_metadata_can_downgrade(metadata_database):
    config,engine=metadata_database;_legacy_records(engine);before=_records(engine)
    command.upgrade(config,REVISION);after=_records(engine)
    columns={c['name']:c for c in inspect(engine).get_columns('agent_runs')}
    for name in FIELDS:
        assert columns[name]['nullable'] and columns[name]['default'] is None
        for row in after['agent_runs']:assert row.pop(name) is None
    assert after==before
    command.downgrade(config,PREVIOUS)
    assert _records(engine)==before
    assert not set(FIELDS)&{c['name'] for c in inspect(engine).get_columns('agent_runs')}


@pytest.mark.parametrize('field',FIELDS)
def test_downgrade_refuses_any_nonnull_field_without_changing_schema_data_or_version(metadata_database,field):
    config,engine=metadata_database;ids=_legacy_records(engine);command.upgrade(config,REVISION)
    with engine.begin() as conn:
        # Unknown future versions are also protected against destructive removal.
        conn.execute(text('UPDATE agent_runs SET '+field+'=CAST(:value AS jsonb) WHERE id=:id'),
                     dict(value='{"schema_version":99}',id=ids['run']))
    before=_records(engine)
    with pytest.raises(RuntimeError,match='Refusing to remove Run metadata'):
        command.downgrade(config,PREVIOUS)
    assert _records(engine)==before
    with engine.connect() as conn:
        assert conn.execute(text('SELECT version_num FROM alembic_version')).scalar_one()==REVISION
    assert set(FIELDS)<={c['name'] for c in inspect(engine).get_columns('agent_runs')}


@pytest.mark.parametrize('value',['[]','"private string"','{"private":"'+'x'*17000+'"}'])
def test_database_rejects_nonobject_or_oversized_metadata(metadata_database,value):
    config,engine=metadata_database;ids=_legacy_records(engine);command.upgrade(config,REVISION)
    with pytest.raises(IntegrityError),engine.begin() as conn:
        conn.execute(text('UPDATE agent_runs SET result_metadata=CAST(:value AS jsonb) WHERE id=:id'),
                     dict(value=value,id=ids['run']))
    with engine.connect() as conn:
        assert conn.execute(text('SELECT result_metadata FROM agent_runs WHERE id=:id'),dict(id=ids['run'])).scalar_one() is None
