from dataclasses import replace
import json
from threading import Barrier
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest

from backend.app.core.errors import BackendError
from backend.app.core.run_metadata import question_metadata, preflight_execution
from backend.app.core.run_metadata import validated
from backend.app.config import RetrievalSettings, StaleReconciliationSettings
from backend.app.services.stale_reconciliation import StaleRunningReconciler
from pycode.question import route_question
from test_b01_users import uow_factory
from test_b02_auth import auth_context
from test_b04_project_derived_auth import resources
from test_b07_worker_isolation import workers, ready
from test_postgresql import postgresql_engine
from test_a01b_semantics import semantic_env
from test_a02b_backend_vector_retrieval import vector_env
from test_a04_question_lifecycle import submit, service, switch_current, expire


def summary(status='insufficient'):
    return dict(status=status,reasons=['evidence_empty'] if status=='insufficient' else [],pack_digest='f'*64,
        selected_count=0 if status=='insufficient' else 1,discarded_count=0,budget=12000,estimated_tokens=100)


@pytest.mark.parametrize('change',[{'schema_version':4},{'strategy':'fast'},
    {'preflight':dict(summary(),query='PRIVATE')},{'preflight':dict(summary(),estimated_tokens=999999)},
    {'preflight':dict(summary(),pack_digest='not-a-digest')}])
def test_v3_metadata_rejects_unsupported_private_and_inconsistent_values(uow_factory,change):
    run=queued(uow_factory);value=preflight_execution(run.execution_metadata,summary())
    with pytest.raises(BackendError):validated('execution_metadata',dict(value,**change))


def queued(factory):
    with factory() as uow,uow.transaction():
        project=uow.projects.create('preflight','https://github.com/example/demo.git')
        snap=uow.snapshots.start(project.id,'a'*40)
        req,exe=question_metadata(snap,route_question('main.py'),None)
        run=uow.agent_runs.create_queued(project.id,snap.id,task='main.py',run_type='question',model=None,
            request_metadata=req,execution_metadata=exe)
        run=uow.agent_runs.mark_running(run.id)
        run=uow.agent_runs.mark_core_started(run.id).run
    return run


def test_preflight_cas_idempotency_identity_and_terminal_atomicity(uow_factory):
    run=queued(uow_factory);exe=preflight_execution(run.execution_metadata,summary())
    with uow_factory() as uow,uow.transaction():
        current=uow.agent_runs.record_question_preflight(run.id,run.execution_metadata,exe)
        assert current.execution_metadata['strategy']=='agent'
        assert uow.agent_runs.record_question_preflight(run.id,run.execution_metadata,exe)==current
        bad=preflight_execution(run.execution_metadata,summary('sufficient'))
        with pytest.raises(BackendError):uow.agent_runs.record_question_preflight(run.id,run.execution_metadata,bad)
    with pytest.raises(RuntimeError),uow_factory() as uow,uow.transaction():
        uow.agent_runs.complete(run.id,'answer');raise RuntimeError('rollback')
    with uow_factory() as uow,uow.transaction():
        current=uow.agent_runs.get(run.id)
        assert current.status=='running' and current.result is None and current.execution_metadata==exe
        done=uow.agent_runs.complete(run.id,'answer')
        assert done.execution_metadata==exe and done.result_metadata['outcome']=='completed'
        with pytest.raises(BackendError):uow.agent_runs.record_question_preflight(run.id,run.execution_metadata,exe)


def test_competing_preflight_transitions_cannot_change_the_first_decision(uow_factory):
    run=queued(uow_factory);barrier=Barrier(2)
    def commit(status):
        barrier.wait(timeout=30)
        try:
            with uow_factory() as uow,uow.transaction():
                return uow.agent_runs.record_question_preflight(run.id,run.execution_metadata,
                    preflight_execution(run.execution_metadata,summary(status)))
        except BackendError:return None
    with ThreadPoolExecutor(max_workers=2) as pool:values=list(pool.map(commit,['sufficient','insufficient']))
    assert sum(value is not None for value in values)==1
    with uow_factory() as uow,uow.transaction():
        current=uow.agent_runs.get(run.id)
        assert current.execution_metadata==next(value.execution_metadata for value in values if value is not None)


def test_post_upgrade_loss_cannot_requeue_or_erase_saved_strategy(vector_env):
    env=vector_env;ids=submit(env,'fast')
    with env.r.context.uow_factory() as uow,uow.transaction():
        task=uow.background_tasks.claim(ids['task_id']).task
        run=uow.agent_runs.mark_running(ids['run_id'])
        run=uow.agent_runs.mark_core_started(run.id).run
        exe=preflight_execution(run.execution_metadata,summary())
        uow.agent_runs.record_question_preflight(run.id,run.execution_metadata,exe)
    switch_current(env);expire(env,task)
    with env.r.context.uow_factory() as uow:
        result=StaleRunningReconciler(uow,env.app.state.project_operations,StaleReconciliationSettings()).reconcile()
        assert result.failed==1 and not env.created
        with uow.transaction():
            current=uow.agent_runs.get(run.id)
            assert current.status=='failed' and current.execution_metadata==exe and current.snapshot_id==env.snapshot_id


def test_lost_execution_token_cannot_persist_a_preflight_or_enter_agent(resources,workers,monkeypatch):
    r=resources;ready(r,workers);app=r.context.app
    from test_b07_worker_isolation import mutate
    from backend.app.db.models import BackgroundTaskORM
    from uuid import uuid4
    ids=r.context.client.post(f'/api/v1/projects/{r.own}/questions',headers=r.headers,json={'question':'main.py'}).json()
    prepare=app.state.adapter.prepare_question
    def revoke(*args,**kwargs):
        pre=prepare(*args,**kwargs)
        with r.context.uow_factory() as uow,uow.transaction():
            mutate(uow,'background_tasks',BackgroundTaskORM,UUID(ids['task_id']),execution_token=uuid4())
        return pre
    def forbidden(*args,**kwargs):pytest.fail('Stale owner must not generate or start Agent')
    monkeypatch.setattr(app.state.adapter,'prepare_question',revoke)
    monkeypatch.setattr(app.state.adapter,'answer_question',forbidden)
    monkeypatch.setattr(app.state.adapter,'run_agent',forbidden)
    workers.agent(ids['task_id'])
    with r.context.uow_factory() as uow,uow.transaction():
        run=uow.agent_runs.get(UUID(ids['run_id']))
        assert run.status=='running' and run.execution_metadata['schema_version']==2 and run.core_started_at
        assert uow.trace_events.list_for_run(run.id)==[]


def test_preflight_error_is_durable_failed_not_an_upgrade_or_model_retry(resources,workers,monkeypatch):
    r=resources;ready(r,workers);app=r.context.app
    calls=[]
    def fail(*args,**kwargs):calls.append(1);raise BackendError('artifacts_unavailable','Unavailable test inputs.')
    def forbidden(*args,**kwargs):pytest.fail('Preflight failure must not reach any answer/Agent')
    monkeypatch.setattr(app.state.adapter,'prepare_question',fail)
    monkeypatch.setattr(app.state.adapter,'answer_question',forbidden)
    monkeypatch.setattr(app.state.adapter,'run_agent',forbidden)
    ids=r.context.client.post(f'/api/v1/projects/{r.own}/questions',headers=r.headers,json={'question':'explain main.py'}).json()
    with pytest.raises(BackendError):workers.agent(ids['task_id'])
    workers.agent(ids['task_id'])
    run=r.context.client.get('/api/v1/agent-runs/'+ids['run_id'],headers=r.headers).json()
    assert len(calls)==1 and run['status']=='failed'
    assert run['execution_metadata']['preflight']['status']=='failed'
    assert run['execution_metadata']['strategy']=='fast' and run['core_started_at']
    events=r.context.client.get('/api/v1/agent-runs/'+ids['run_id']+'/trace',headers=r.headers).json()['events']
    assert [e['event_type'] for e in events]==['run_started','context_updated','run_failed']


def test_actual_vector_upgrade_reuses_original_pack_and_keeps_snapshot_across_redelivery(vector_env,monkeypatch):
    env=vector_env;ids=submit(env,'fast');switch_current(env)
    original=[];prompts=[];seen=[]
    prepare=env.app.state.adapter.prepare_question
    def capture(*args,**kwargs):
        result=prepare(*args,**kwargs);original.append(result);return result
    monkeypatch.setattr(env.app.state.adapter,'prepare_question',capture)
    def llm(prompt):
        prompts.append(prompt)
        if prompt.startswith('You are the PyCode LLM Planner.'):
            payload=json.loads(prompt.split('Planning input:\n')[-1]);seen.append(payload['initial_evidence'])
            return json.dumps([dict(tool='retrieve_context',arguments={'question':'return'},reason='inspect initial',required=False)])
        if prompt.startswith('You are the PyCode LLM Next-Action Planner.'):
            turn=json.loads(prompt.split('Next-action input:\n')[-1])['turn']['turn_index']
            if turn<=2:
                return json.dumps(dict(action_type='tool_call',tool_name='retrieve_context',
                    arguments={'question':'return' if turn==1 else 'a different topic'},reason='verify'))
            return json.dumps(dict(action_type='final_answer',final_answer='Current evidence is limited.',reason='verified'))
        return '[]'
    monkeypatch.setattr(env.app.state.adapter.llm_client,'generate',llm)
    with env.r.context.uow_factory() as uow:
        worker=service(env,uow)
        worker.projects.semantics.retrieval_settings_factory=lambda:RetrievalSettings('vector',token_budget=4096)
        run=worker.execute(ids['task_id']);count=len(env.created)
        assert worker.execute(ids['task_id'])==run and len(env.created)==count
    assert run.status=='completed' and len(original)==1
    assert run.execution_metadata['schema_version']==3 and run.execution_metadata['strategy']=='agent'
    assert run.snapshot_id==env.snapshot_id
    pre=original[0];assert pre.status=='insufficient' and pre.initial_evidence.result.items
    assert seen[0]['initial_pack_digest']==pre.initial_evidence.digest
    assert seen[0]['items']==pre.initial_evidence.projection()['items']
    assert len(env.created)==2 and all(p.closed for p in env.created)
    assert sum(len(p.queries) for p in env.created)==2  # preflight + one genuinely new query
    assert run.result_metadata['references']
    with env.r.context.uow_factory() as uow,uow.transaction():events=uow.trace_events.list_for_run(run.id)
    payloads=[e.payload['event_payload'] for e in events]
    reused=[p for p in payloads if p.get('retrieval_origin')=='question_preflight']
    assert len(reused)==1 and all(p['initial_pack_digest']==pre.initial_evidence.digest for p in reused)
    assert payloads[-1]['trace_complete'] is True
