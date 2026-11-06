"""A05c bounded metadata, two UoWs, real authorized Runtime/Worker and replay."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
import json
from threading import Barrier
from uuid import UUID

import pytest

from backend.app.core.application_trace import ApplicationTraceNormalizer,public_harness_budget
from backend.app.core.errors import BackendError
from backend.app.core.run_metadata import result_for,validated
from pycode.agent.budget import ExecutionBudget,current_budget
from pycode.agent.context_budget import ContextUnit,make_report,public_context_report
from pycode.agent.evidence import verify_context_answer
from pycode.chunks import SnapshotBinding
from test_a01b_semantics import semantic_env
from test_a02b_backend_vector_retrieval import vector_env
from test_a04_question_lifecycle import submit,service,switch_current
from test_a04_run_metadata import create_run
from test_b01_users import uow_factory
from test_b02_auth import auth_context
from test_b04_project_derived_auth import resources
from test_postgresql import postgresql_engine


def metadata(run,snapshot,*,failed=False,path='logic.py'):
    budget=ExecutionBudget(binding=SnapshotBinding(str(snapshot.project_id),str(snapshot.id),snapshot.commit_sha))
    unit=ContextUnit('Synthetic source',dict(source='snapshot_source',tool='read_file',turn=1,path=path,
        symbol=None,start_line=1,end_line=2,chunk_ids=[],chunk_ids_omitted=0,coverage='complete_file',content_hash='a'*64))
    with budget.activate():report=make_report('summary','Synthetic model input',[unit],[])
    report['delivered_to_model']=True
    verification=verify_context_answer(path+':1-2',report)
    return result_for(run,context_report=report,verification=verification,failed=failed)


@pytest.mark.parametrize('failed',[False,True])
def test_v2_text_metadata_rollback_history_and_terminal_state_are_atomic(uow_factory,failed):
    snapshot,run=create_run(uow_factory)
    value=metadata(run,snapshot,failed=failed)
    assert value['schema_version']==2 and len(json.dumps(value,ensure_ascii=False).encode())<16384
    def terminal(uow):
        return uow.agent_runs.fail(run.id,'safe failure',result_metadata=value) if failed else uow.agent_runs.complete(run.id,'atomic answer',result_metadata=value)
    with pytest.raises(RuntimeError),uow_factory() as uow,uow.transaction():
        terminal(uow);raise RuntimeError('Force rollback')
    with uow_factory() as uow,uow.transaction():
        current=uow.agent_runs.get(run.id)
        assert current.status=='running' and current.result is None and current.result_metadata is None
        done=terminal(uow)
    value['context']['references'][0]['path']='mutated.py'
    with uow_factory() as uow,uow.transaction():
        persisted=uow.agent_runs.get(run.id)
        assert persisted==done and persisted.result== (None if failed else 'atomic answer')
        assert persisted.result_metadata['context']['references'][0]['path']=='logic.py'
        assert uow.agent_runs.list_history_for_project(run.project_id,10,0)==([] if failed else [persisted])
        with pytest.raises(BackendError):terminal(uow)


def test_competing_v2_terminal_writes_cannot_mix_answer_and_selected_sources(uow_factory):
    snapshot,run=create_run(uow_factory);barrier=Barrier(2)
    def write(label):
        value=metadata(run,snapshot,path=label+'.py');barrier.wait()
        try:
            with uow_factory() as uow,uow.transaction():
                return uow.agent_runs.complete(run.id,label,result_metadata=value)
        except BackendError as exc:
            assert exc.code=='agent_run_state_conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(write,['one','two']))
    assert sum(r is not None for r in results)==1
    with uow_factory() as uow,uow.transaction():
        current=uow.agent_runs.get(run.id)
        assert current.result_metadata['references'][0]['path']==current.result+'.py'


@pytest.mark.parametrize('change',[
    lambda v:v.update(schema_version=3),
    lambda v:v['context'].update(prompt='PRIVATE'),
    lambda v:v['context'].update(budget=True),
    lambda v:v['context']['binding'].update(snapshot_id='00000000-0000-0000-0000-000000000001'),
    lambda v:v['verification'].update(natural_language_verified=True),
    lambda v:v['context']['references'][0].update(path='../outside.py'),
])
def test_v2_invalid_private_or_cross_binding_metadata_never_writes_half_result(uow_factory,change):
    snapshot,run=create_run(uow_factory);value=metadata(run,snapshot);change(value)
    with pytest.raises(BackendError) as error,uow_factory() as uow,uow.transaction():
        uow.agent_runs.complete(run.id,'must not persist',result_metadata=value)
    assert error.value.code=='invalid_run_metadata' and 'PRIVATE' not in str(error.value)
    with uow_factory() as uow,uow.transaction():
        current=uow.agent_runs.get(run.id)
        assert current.status=='running' and current.result is None and current.result_metadata is None


def test_legacy_null_v1_and_reserved_budget_traces_remain_readable(uow_factory):
    for enabled in (False,True):
        _,run=create_run(uow_factory,metadata=enabled)
        with uow_factory() as uow,uow.transaction():
            done=uow.agent_runs.complete(run.id,'legacy answer')
            assert done.result_metadata is None if not enabled else done.result_metadata['schema_version']==1
    legacy=ExecutionBudget().snapshot();legacy['context_output_enforcement']='reserved_for_a05c'
    assert public_harness_budget(legacy)==legacy


@pytest.mark.parametrize('output_limit',[32768,256])
@pytest.mark.parametrize('legacy_wrapper',[False,True])
def test_real_worker_fixed_snapshot_actual_selection_v2_and_replay(vector_env,monkeypatch,output_limit,legacy_wrapper):
    env=vector_env;monkeypatch.setenv('PYCODE_AGENT_TOOL_OUTPUT_CHARS',str(output_limit))
    ids=submit(env,'agent');newer=switch_current(env);models=[];captured=[]
    original=env.app.state.adapter.run_agent
    def record(*a,**kw):
        result=original(*a,**kw);captured.append(result);return result
    def legacy_record(root,task,*,trace_observer=None,model=None):
        return record(root,task,trace_observer=trace_observer,model=model)
    monkeypatch.setattr(env.app.state.adapter,'run_agent',legacy_record if legacy_wrapper else record)
    def model(prompt):
        budget=current_budget()
        assert not env.active_transactions and not env.memory_transactions
        assert budget.binding.snapshot_id==str(env.snapshot_id)
        models.append(prompt)
        if prompt.startswith('You are the PyCode LLM Planner.'):
            assert json.loads(prompt.split('Planning input:\n')[-1])['task']['binding']==asdict(budget.binding)
            return json.dumps([dict(tool='retrieve_context',arguments={'question':'return'},required=False)])
        if prompt.startswith('You are the PyCode LLM Next-Action Planner.'):
            payload=json.loads(prompt.split('Next-action input:\n')[-1])
            assert payload['task']['binding']==asdict(budget.binding)
            turn=payload['turn']['turn_index']
            if turn==1:return json.dumps(dict(action_type='tool_call',tool_name='retrieve_context',arguments={'question':'return'}))
            return json.dumps(dict(action_type='final_answer',final_answer='See main.py for source; omitted evidence remains uncertain.'))
        return '[]'
    monkeypatch.setattr(env.app.state.adapter.llm_client,'generate',model)
    with env.r.context.uow_factory() as uow:
        worker=service(env,uow);run=worker.execute(ids['task_id']);before=len(models)
        assert worker.execute(ids['task_id'])==run and len(models)==before
        with uow.transaction():assert uow.snapshots.get(env.snapshot_id).status=='ready'
    value=run.result_metadata
    assert run.status=='completed' and run.snapshot_id==env.snapshot_id!=newer.id
    assert value['schema_version']==2 and value['context']['binding']['snapshot_id']==str(env.snapshot_id)
    assert value['context']['phase']=='next_action' and value['context']['delivered_to_model']
    assert value['verification']['natural_language_verified'] is False
    if output_limit==256:
        assert not value['references'] and value['context']['reason_counts']['tool_output_limit']>0
        assert captured[0].tool_results[0].data['items']  # original Pack preserved
    else:assert value['references']
    response=env.r.context.client.get(f'/api/v1/agent-runs/{run.id}/trace',headers=env.r.headers)
    events=response.json()['events'];terminal=events[-1]['payload']
    stages=[e['payload']['harness_context'] for e in events if e['event_type']=='llm_finished'
            and e['payload']['purpose'] in {'plan','next_action'}]
    assert len(stages)==3 and all(c['delivered_to_model'] and c['estimated_tokens']<=c['budget'] for c in stages)
    assert terminal['harness_context']==value['context'] and terminal['verification']==value['verification']
    offline=ApplicationTraceNormalizer().normalize(run,result=captured[0])
    assert offline.events[-1].payload['harness_context']==terminal['harness_context']
    public=env.r.context.client.get(f'/api/v1/agent-runs/{run.id}',headers=env.r.headers).json()
    assert public['result_metadata']==value
    assert env.r.context.client.get(f'/api/v1/agent-runs/{run.id}',headers=env.r.other_headers).status_code==404
    assert len(env.created)==1 and all(p.closed for p in env.created)


def test_unicode_context_and_reference_limits_fit_real_jsonb_contract(uow_factory):
    snapshot,run=create_run(uow_factory)
    budget=ExecutionBudget(binding=SnapshotBinding(str(snapshot.project_id),str(snapshot.id),snapshot.commit_sha))
    units=[ContextUnit('Synthetic',dict(source='snapshot_source',tool='read_file',turn=i+1,
        path=('源'*390)+str(i)+'.py',start_line=1,end_line=2,symbol=None,chunk_ids=[],chunk_ids_omitted=0,
        coverage='complete_file',content_hash='a'*64)) for i in range(8)]
    with budget.activate():report=make_report('summary','Rendered',units,[])
    value=result_for(run,context_report=report,verification=verify_context_answer(None,report))
    assert value['context']['references_omitted']>0 and len(json.dumps(value,ensure_ascii=False).encode())<14000
    with uow_factory() as uow,uow.transaction():
        done=uow.agent_runs.complete(run.id,'answer',result_metadata=value)
        assert done.result_metadata==value


def test_real_worker_minimum_context_failure_never_calls_models_and_preserves_ready(vector_env,monkeypatch):
    env=vector_env;monkeypatch.setenv('PYCODE_AGENT_CONTEXT_TOKENS','4096')
    ids=submit(env,'agent')
    def forbidden(prompt):pytest.fail('Oversized minimum input reached a model')
    monkeypatch.setattr(env.app.state.adapter.llm_client,'generate',forbidden)
    with env.r.context.uow_factory() as uow:
        worker=service(env,uow)
        with pytest.raises(BackendError):worker.execute(ids['task_id'])
        run=worker.get(ids['run_id'])
        assert worker.execute(ids['task_id'])==run
        with uow.transaction():assert uow.snapshots.get(env.snapshot_id).status=='ready'
    assert run.status=='failed' and not env.created
    value=run.result_metadata
    assert value['schema_version']==2 and not value['context']['delivered_to_model']
    assert value['context']['estimated_tokens']>value['context']['budget']==4096
    events=env.r.context.client.get(f'/api/v1/agent-runs/{run.id}/trace',headers=env.r.headers).json()['events']
    assert events[-1]['payload']['harness_budget']['stop_reason']=='budget_context'
