"""Real Runtime, authorized Worker, two persistence implementations; no paid calls."""
import errno
import json
from dataclasses import replace
from uuid import UUID,uuid4

import pytest

from backend.app.core.application_trace import (
    ApplicationTraceNormalizer, public_harness_execution, public_tool_execution,
)
from backend.app.core.errors import BackendError
from backend.app.db.models import BackgroundTaskORM
from pycode.agent.budget import current_budget
from pycode.tools import TOOLS
from test_a01b_semantics import semantic_env
from test_a02b_backend_vector_retrieval import vector_env
from test_a04_question_lifecycle import submit, service, switch_current
from test_b01_users import uow_factory
from test_b02_auth import auth_context
from test_b04_project_derived_auth import resources
from test_b07_worker_isolation import workers, ready, mutate
from test_postgresql import postgresql_engine


def trace(r,run_id):
    response=r.context.client.get(f'/api/v1/agent-runs/{run_id}/trace',headers=r.headers)
    assert response.status_code==200
    return response.json()['events']


@pytest.mark.parametrize('failure_kind',['required','repeat'])
def test_fixed_snapshot_partial_evidence_failure_is_atomic_durable_and_replayable(vector_env,monkeypatch,failure_kind):
    env=vector_env
    ids=submit(env,'agent')
    newer=switch_current(env)
    models=[]; observed=[]
    original=env.app.state.adapter.run_agent
    def capture(*a,**kw):
        result=original(*a,**kw)
        observed.append(result)
        return result
    monkeypatch.setattr(env.app.state.adapter,'run_agent',capture)
    def generate(prompt):
        assert not env.active_transactions and not env.memory_transactions
        assert current_budget().binding.snapshot_id==str(env.snapshot_id)
        models.append(prompt)
        if prompt.startswith('You are the PyCode LLM Planner.'):
            return json.dumps([dict(tool='retrieve_context',arguments={'question':'return'},required=True),
                               dict(tool='read_file',arguments={'file_path':'missing.py'},required=failure_kind=='required')])
        if prompt.startswith('You are the PyCode LLM Next-Action Planner.'):
            turn=json.loads(prompt.split('Next-action input:\n')[-1])['turn']['turn_index']
            if turn==1:return json.dumps(dict(action_type='tool_call',tool_name='retrieve_context',arguments={'question':'return'}))
            if turn==2 or failure_kind=='repeat':
                return json.dumps(dict(action_type='tool_call',tool_name='read_file',arguments={'file_path':'missing.py'}))
            return json.dumps(dict(action_type='final_answer',final_answer='Cannot erase missing required evidence'))
        pytest.fail('Failed/no-progress execution attempted summary or Memory extraction')
    monkeypatch.setattr(env.app.state.adapter.llm_client,'generate',generate)
    with env.r.context.uow_factory() as uow:
        worker=service(env,uow)
        with pytest.raises(BackendError):worker.execute(ids['task_id'])
        run=worker.get(ids['run_id'])
        before=(len(models),len(env.created))
        assert worker.execute(ids['task_id'])==run and before==(len(models),len(env.created))
        with uow.transaction():
            task=uow.background_tasks.get(ids['task_id'])
            assert task.status=='failed' and task.attempt_count==1
            assert uow.snapshots.get(env.snapshot_id).status=='ready'
            records=uow.trace_events.list_for_run(run.id)
    assert run.status=='failed' and run.result is None and run.snapshot_id==env.snapshot_id!=newer.id
    assert run.result_metadata['outcome']=='failed' and run.result_metadata['references']
    assert run.result_metadata['binding']['snapshot_id']==str(env.snapshot_id)
    assert len(observed)==1 and not observed[0].ok
    events=trace(env.r,run.id)
    report=events[-1]['payload']['harness_execution']
    assert report['outcome']=='partial' and report['completed_tools']==1
    assert report['stop_reason']==('required_tool_failed' if failure_kind=='required' else 'no_progress')
    assert report['failed_tools']==(1 if failure_kind=='required' else 2)
    assert public_harness_execution(report)==report
    assert len(records)==len(events) and events[-1]['event_type']=='run_failed'
    failures=[e for e in events if e['event_type']=='tool_finished' and e['payload']['outcome']=='failed']
    assert failures and all(e['payload']['execution']['category']=='not_found' for e in failures)
    assert all(e['payload']['execution']['attempts']==1 for e in failures)
    assert 'missing.py' not in json.dumps(report)
    assert all(p.closed for p in env.created) and current_budget() is None
    # Offline normalization must preserve exactly the same safe execution reports.
    normalized=ApplicationTraceNormalizer().normalize(run,result=observed[0])
    assert normalized.events[-1].payload['harness_execution']==report
    denied=env.r.context.client.get(f'/api/v1/agent-runs/{run.id}/trace',headers=env.r.other_headers)
    assert denied.status_code==404


def test_unknown_versions_unbounded_counters_and_private_data_are_rejected():
    tool=dict(schema_version=1,category='failed',attempts=1,retryable=False,truncated=False)
    report=dict(schema_version=1,outcome='failed',tool_calls=1,handler_attempts=1,retried_tools=0,
                completed_tools=0,failed_tools=1,empty_tools=0,required_failed=1,stop_reason='required_tool_failed')
    assert public_tool_execution(tool)==tool and public_harness_execution(report)==report
    for change in (dict(schema_version=2),dict(attempts=True),dict(attempts=3),dict(prompt='private')):
        assert public_tool_execution({**tool,**change}) is None
    for change in (dict(schema_version=2),dict(tool_calls=True),dict(tool_calls=513),
                   dict(stop_reason='private'),dict(prompt='private'),dict(completed_tools=1)):
        assert public_harness_execution({**report,**change}) is None


@pytest.mark.parametrize('lose_ownership',[False,True])
def test_real_worker_read_retry_obeys_fixed_binding_and_lease(vector_env,monkeypatch,lose_ownership):
    env=vector_env;ids=submit(env,'agent');switch_current(env)
    original=TOOLS['read_file'];calls=[]
    def transient(context,**kwargs):
        assert context.retrieval_context.binding.snapshot_id==str(env.snapshot_id)
        assert not env.active_transactions and not env.memory_transactions
        calls.append(1)
        if len(calls)==1:
            if lose_ownership:
                with env.r.context.uow_factory() as uow,uow.transaction():
                    mutate(uow,'background_tasks',BackgroundTaskORM,ids['task_id'],execution_token=uuid4())
            raise OSError(errno.EAGAIN,'Synthetic transient read')
        return original.handler(context,**kwargs)
    monkeypatch.setitem(TOOLS,'read_file',replace(original,handler=transient))
    def model(prompt):
        assert not env.active_transactions and not env.memory_transactions
        if prompt.startswith('You are the PyCode LLM Planner.'):
            return json.dumps([dict(tool='read_file',arguments={'file_path':'main.py'},required=True)])
        if prompt.startswith('You are the PyCode LLM Next-Action Planner.'):
            turn=json.loads(prompt.split('Next-action input:\n')[-1])['turn']['turn_index']
            return json.dumps(dict(action_type='tool_call',tool_name='read_file',arguments={'file_path':'main.py'})) if turn==1 else json.dumps(dict(action_type='final_answer',final_answer='Read confirmed'))
        return '[]'
    monkeypatch.setattr(env.app.state.adapter.llm_client,'generate',model)
    with env.r.context.uow_factory() as uow:
        worker=service(env,uow)
        run=worker.execute(ids['task_id'])
        with uow.transaction():records=uow.trace_events.list_for_run(run.id)
    if lose_ownership:
        assert calls==[1] and run.status=='running' and run.result is None and records==[]
    else:
        assert calls==[1,1] and run.status=='completed'
        events=trace(env.r,run.id)
        execution=next(e['payload']['execution'] for e in events if e['event_type']=='tool_finished')
        assert execution['attempts']==2 and execution['category']=='success'
        terminal=events[-1]['payload']
        assert terminal['harness_execution']['handler_attempts']==terminal['harness_budget']['usage']['tool_calls']==2
        assert terminal['harness_execution']['retried_tools']==1


def test_unauthorized_question_never_enters_tool_or_budget(resources,workers,monkeypatch):
    r=resources;ready(r,workers)
    def forbidden(*a,**kw):pytest.fail('Unauthorized request reached Runtime')
    monkeypatch.setattr(r.context.app.state.adapter,'execution_scope',forbidden)
    response=r.context.client.post(f'/api/v1/projects/{r.own}/questions',headers=r.other_headers,
        json={'question':'cross-module call chain'})
    assert response.status_code==404
