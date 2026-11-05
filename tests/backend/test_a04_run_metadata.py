from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime,timezone
from threading import Barrier

import pytest

from backend.app.core.errors import BackendError
from backend.app.core.run_metadata import legacy_request,legacy_execution,result_for
from dataclasses import replace
from sqlalchemy import update
from backend.app.db.models import AgentRunORM
from test_b01_users import uow_factory
from test_postgresql import postgresql_engine


def create_run(factory,queued=False,metadata=True):
    with factory() as uow,uow.transaction():
        project=uow.projects.create('Metadata','https://github.com/example/demo.git')
        snapshot=uow.snapshots.start(project.id,'a'*40)
        req=legacy_request(snapshot,'agent' if queued else 'ask') if metadata else None
        create=uow.agent_runs.create_queued if queued else uow.agent_runs.create
        run=create(project.id,snapshot.id,task='original task',run_type='agent' if queued else 'ask',model=None,
            request_metadata=req,execution_metadata=None if queued else legacy_execution(req))
    return snapshot,run


@pytest.mark.parametrize('failed',[False,True])
def test_terminal_text_metadata_rollback_copy_and_history_are_one_contract(uow_factory,failed):
    snapshot,run=create_run(uow_factory)
    with pytest.raises(RuntimeError),uow_factory() as uow,uow.transaction():
        if failed:uow.agent_runs.fail(run.id,'safe error')
        else:uow.agent_runs.complete(run.id,'atomic answer')
        raise RuntimeError('force transaction rollback')
    with uow_factory() as uow,uow.transaction():
        current=uow.agent_runs.get(run.id)
        assert current.status=='running' and current.result is None and current.result_metadata is None
        value=uow.agent_runs.fail(run.id,'safe error') if failed else uow.agent_runs.complete(run.id,'atomic answer')
        assert value.result_metadata['outcome']==('failed' if failed else 'completed')
        assert value.result== (None if failed else 'atomic answer')
    value.request_metadata['binding']['commit_sha']='b'*40
    value.result_metadata['diagnostics'].append('MUTATED')
    with uow_factory() as uow,uow.transaction():
        persisted=uow.agent_runs.get(run.id)
        assert persisted.request_metadata['binding']['commit_sha']==snapshot.commit_sha
        assert 'MUTATED' not in persisted.result_metadata['diagnostics']
        history=uow.agent_runs.list_history_for_project(run.project_id,10,0)
        assert history==([] if failed else [persisted])
        with pytest.raises(BackendError) as error:
            uow.agent_runs.complete(run.id,'overwrite')
        assert error.value.code=='agent_run_state_conflict'


@pytest.mark.parametrize('failure',['unknown','binding','outcome','oversize'])
def test_invalid_metadata_never_leaves_a_half_terminal_result(uow_factory,failure):
    snapshot,run=create_run(uow_factory);meta=result_for(run)
    if failure=='unknown':meta['schema_version']=2
    elif failure=='binding':meta['binding']['commit_sha']='b'*40
    elif failure=='outcome':meta['outcome']='failed'
    else:meta['private']='x'*17000
    with pytest.raises(BackendError) as error,uow_factory() as uow,uow.transaction():
        uow.agent_runs.complete(run.id,'must not persist',result_metadata=meta)
    assert error.value.code=='invalid_run_metadata'
    with uow_factory() as uow,uow.transaction():
        old=uow.agent_runs.get(run.id)
        assert old.status=='running' and old.result is None and old.result_metadata is None


def test_queued_retry_keeps_identity_and_only_new_runs_get_metadata(uow_factory):
    _,run=create_run(uow_factory,queued=True)
    with uow_factory() as uow,uow.transaction():
        running=uow.agent_runs.mark_running(run.id)
        assert running.execution_metadata['strategy']=='agent'
        queued=uow.agent_runs.requeue_running(run.id)
        assert queued.request_metadata==run.request_metadata and queued.result_metadata is None
        assert queued.execution_metadata==running.execution_metadata
        running=uow.agent_runs.mark_running(run.id)
        uow.agent_runs.mark_core_started(run.id)
        with pytest.raises(BackendError):uow.agent_runs.requeue_running(run.id)
    _,legacy=create_run(uow_factory,metadata=False)
    with uow_factory() as uow,uow.transaction():
        legacy=uow.agent_runs.complete(legacy.id,'old text, no evidence inferred')
        assert legacy.request_metadata is None and legacy.result_metadata is None


def test_concurrent_complete_and_fail_have_one_consistent_winner(uow_factory):
    _,run=create_run(uow_factory);barrier=Barrier(2)
    def finish(failed):
        barrier.wait(timeout=10)
        try:
            with uow_factory() as uow,uow.transaction():
                value=uow.agent_runs.fail(run.id,'winner failure') if failed else uow.agent_runs.complete(run.id,'winner answer')
                return value.status
        except BackendError as exc:return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(finish,[False,True]))
    assert results.count('agent_run_state_conflict')==1
    with uow_factory() as uow,uow.transaction():
        final=uow.agent_runs.get(run.id)
        assert final.status in {'completed','failed'} and final.result_metadata['outcome']==final.status
        assert (final.result=='winner answer')==(final.status=='completed')
        assert (final.error_message=='winner failure')==(final.status=='failed')


def test_competing_completions_cannot_mix_answer_with_other_writers_metadata(uow_factory):
    _,run=create_run(uow_factory);barrier=Barrier(2)
    def finish(intent):
        metadata=result_for(run,intent=intent);barrier.wait(timeout=10)
        try:
            with uow_factory() as uow,uow.transaction():
                return uow.agent_runs.complete(run.id,intent,result_metadata=metadata).status
        except BackendError as exc:return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:outcomes=list(pool.map(finish,['general','impact']))
    assert sorted(outcomes)==['agent_run_state_conflict','completed']
    with uow_factory() as uow,uow.transaction():
        value=uow.agent_runs.get(run.id)
        assert value.result==value.result_metadata['intent']


def test_request_cannot_claim_another_snapshot_commit(uow_factory):
    snapshot,run=create_run(uow_factory);forged=deepcopy(run.request_metadata)
    forged['binding']['commit_sha']='f'*40
    with pytest.raises(BackendError),uow_factory() as uow,uow.transaction():
        uow.agent_runs.create(run.project_id,snapshot.id,task='forged',run_type='ask',model=None,
            request_metadata=forged,execution_metadata=legacy_execution(forged))
    with uow_factory() as uow,uow.transaction():
        assert [r.id for r in uow.agent_runs.list_for_project(run.project_id)]==[run.id]


@pytest.mark.parametrize('queued',[False,True])
@pytest.mark.parametrize('field',['request_metadata','execution_metadata','result_metadata'])
def test_quarantine_terminates_only_invalid_inflight_data_without_erasing_it(uow_factory,queued,field):
    _,run=create_run(uow_factory,queued=queued)
    raw={'schema_version':99,'private':'preserve in database only'}
    with uow_factory() as uow,uow.transaction():
        values={field:raw,'result':'preserve original text'}
        if getattr(uow,'session',None) is not None:
            uow.session.execute(update(AgentRunORM).where(AgentRunORM.id==run.id).values(**values))
        else:uow.state.agent_runs[run.id]=replace(uow.state.agent_runs[run.id],**values)
    with uow_factory() as uow,uow.transaction():
        before=uow.agent_runs.get(run.id)
        failed=uow.agent_runs.quarantine_invalid(run.id,'safe terminal error')
        assert failed.status=='failed' and failed.started_at and failed.finished_at
        assert failed.result==before.result=='preserve original text'
        for name in ('request_metadata','execution_metadata','result_metadata'):
            assert getattr(failed,name)==getattr(before,name)
    with pytest.raises(BackendError),uow_factory() as uow,uow.transaction():
        uow.agent_runs.quarantine_invalid(run.id,'repeat must not overwrite')


def test_quarantine_cannot_be_used_to_bypass_a_valid_contract(uow_factory):
    _,run=create_run(uow_factory)
    with pytest.raises(BackendError),uow_factory() as uow,uow.transaction():
        uow.agent_runs.quarantine_invalid(run.id,'must not bypass normal transition')
    with uow_factory() as uow,uow.transaction():assert uow.agent_runs.get(run.id)==run
