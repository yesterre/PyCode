"""Memory lifecycle through authorized real Worker, both UoWs and safe replay."""
from contextlib import contextmanager
from dataclasses import replace
import json
from uuid import uuid4

import pytest

from backend.app.tasks import agent_runs
from backend.app.core.application_trace import ApplicationTraceEvent,ApplicationEventType,application_event_from_record
from pycode.agent.memory import MemoryStore,MemoryRunInfo
from pycode.agent.memory_record import MemoryScope
from pycode.agent.budget import current_budget
from pycode.chunks import SnapshotBinding
from test_a01b_semantics import semantic_env
from test_a02b_backend_vector_retrieval import vector_env
from test_a04_question_lifecycle import submit,service,switch_current
from test_b01_users import uow_factory
from test_b02_auth import auth_context
from test_b04_project_derived_auth import resources
from test_postgresql import postgresql_engine


@pytest.mark.parametrize('failure',[False,True,'gated','deleted','invalidated'])
def test_worker_uses_original_snapshot_and_project_declarations_with_safe_memory_trace(vector_env,monkeypatch,failure):
    env=vector_env;ids=submit(env,'agent');root=env.app.state.workspace.path_for(env.r.own)
    old=MemoryScope(root,SnapshotBinding(str(env.r.own),str(uuid4()),'a'*40),str(uuid4()))
    declared=MemoryStore(root,scope=old,origin='user_explicit').add_memory(memory_id='context-note',
        memory_type='preference',title='Cross module policy',summary='Cross module context',body='Use targeted pytest')
    with old.activate():
        MemoryStore(root,origin='model_inference').add_memory(memory_id='stale',memory_type='analysis',
            title='Old code fact',summary='Old',body='PRIVATE_STALE_CODE')
    if failure in {'deleted','invalidated'}:
        bound=MemoryScope(root,SnapshotBinding(str(env.r.own),str(env.snapshot_id),'a'*40),str(uuid4()))
        store=MemoryStore(root,scope=bound,origin='model_inference')
        withdrawn=store.add_memory(memory_id='withdrawn',memory_type='analysis',title='New inference',
                                   summary='',body='PRIVATE_NEW_HINT')
        store.manage_memory(withdrawn.id,'delete' if failure=='deleted' else 'invalidate')
    newer=switch_current(env);calls=[]
    def generate(prompt):
        assert not env.active_transactions and not env.memory_transactions
        assert current_budget().binding.snapshot_id==str(env.snapshot_id)
        assert 'PRIVATE_STALE_CODE' not in prompt
        calls.append(prompt)
        if prompt.startswith('Select project memories'):
            if failure is True:raise RuntimeError('PRIVATE_PROVIDER_ERROR')
            return '["context-note","stale","context-note"]'
        if prompt.startswith('You are the PyCode LLM Planner.'):
            assert 'Use targeted pytest' in prompt
            return json.dumps([dict(tool='retrieve_context',arguments={'question':'return'},required=False)])
        if prompt.startswith('You are the PyCode LLM Next-Action Planner.'):
            turn=json.loads(prompt.split('Next-action input:\n')[-1])['turn']['turn_index']
            if turn==1:return json.dumps(dict(action_type='tool_call',tool_name='retrieve_context',arguments={'question':'return'}))
            return json.dumps(dict(action_type='final_answer',final_answer='token=FAKE_GATE' if failure=='gated' else 'Current Snapshot analysis uses main.py.'))
        if prompt.startswith('Extract durable PyCode memories'):
            if failure is True:raise RuntimeError('PRIVATE_PROVIDER_ERROR')
            return json.dumps([dict(id='new-note',type='analysis',title='New inference',body='PRIVATE_NEW_HINT',origin='user_explicit')])
        return '[]'
    monkeypatch.setattr(env.app.state.adapter.llm_client,'generate',generate)
    @contextmanager
    def services():
        with env.r.context.uow_factory() as uow:yield service(env,uow)
    monkeypatch.setattr(agent_runs,'worker_agent_run_service',services)
    agent_runs.run_agent_task.run(str(ids['task_id']));before=len(calls)
    agent_runs.run_agent_task.run(str(ids['task_id']))
    assert len(calls)==before
    with env.r.context.uow_factory() as uow,uow.transaction():
        run=uow.agent_runs.get(ids['run_id'])
        assert run.status=='completed' and run.snapshot_id==env.snapshot_id!=newer.id
        assert uow.snapshots.get(env.snapshot_id).status=='ready'
        rows=uow.trace_events.list_for_run(run.id)
    terminal=application_event_from_record(rows[-1]);summary=terminal.payload['memory']
    assert terminal.payload['trace_complete'] is True
    if failure=='gated':assert summary['reason_counts']['input_sensitive']==1
    if failure in {'deleted','invalidated'}:assert summary['reason_counts']['inactive']>=1
    assert summary['selected_count']==1
    assert summary['source_counts']=={'user_explicit':1}
    assert summary['references'][0]['record_id']==declared.record_id
    assert summary['reason_counts']['snapshot_mismatch']>=1
    assert summary['created_count']==(0 if failure else 1)
    response=env.r.context.client.get(f'/api/v1/agent-runs/{run.id}/trace',headers=env.r.headers)
    assert response.status_code==200 and response.json()['events'][-1]['payload']['memory']==summary
    encoded=json.dumps([r.payload for r in rows])
    assert all(text not in encoded for text in ('PRIVATE_STALE_CODE','PRIVATE_NEW_HINT','PRIVATE_PROVIDER_ERROR','Use targeted pytest'))
    assert env.r.context.client.get(f'/api/v1/agent-runs/{run.id}/trace',headers=env.r.other_headers).status_code==404
    with MemoryScope(root,SnapshotBinding(str(env.r.own),str(env.snapshot_id),'a'*40),str(ids['run_id'])).activate():
        available=MemoryStore(root).list_memories()
        assert {e.id for e in available}==({'context-note'} if failure else {'context-note','new-note'})
        if not failure:
            note=MemoryStore(root).load_memory('new-note')
            assert note.provenance['origin']=='model_inference'
            assert note.provenance['run_id']==str(run.id)


def test_public_memory_summary_is_strict_and_old_missing_field_still_valid():
    from datetime import datetime,timezone
    now=datetime.now(timezone.utc)
    base=dict(stop_reason='final',has_result=True,trace_complete=True)
    assert ApplicationTraceEvent(0,ApplicationEventType.RUN_COMPLETED,now,None,base).payload==base
    summary=MemoryRunInfo().public_summary()
    for invalid in (dict(summary,body='PRIVATE'),dict(summary,schema_version=True),
                    dict(summary,reason_counts={'PRIVATE':1}),dict(summary,references=[{'body':'PRIVATE'}])):
        with pytest.raises(ValueError):ApplicationTraceEvent(0,ApplicationEventType.RUN_COMPLETED,now,None,dict(base,memory=invalid))


def test_memory_attempt_flags_do_not_fabricate_model_calls_and_legacy_defaults_hold():
    from backend.app.core.application_trace import _llm_observation_counts
    def event(kind,data):return type('Event',(),dict(event_type=kind,data=data))()
    skipped=[event('MemoryExtracted',{'model_attempted':False}),event('MemoryRelevantLoaded',{'model_attempted':False})]
    assert not +_llm_observation_counts(skipped)[2]
    expected=_llm_observation_counts([event('MemoryExtracted',{}),event('MemoryRelevantLoaded',{})])[2]
    assert expected['memory_extraction']==expected['memory_selection']==1


def test_optional_memory_stats_cannot_break_existing_trace_payload_size_boundary():
    from datetime import datetime,timezone
    from backend.app.core.application_trace import MAX_PUBLIC_PAYLOAD_BYTES
    now=datetime.now(timezone.utc)
    base=dict(stop_reason='x'*(MAX_PUBLIC_PAYLOAD_BYTES-100),has_result=True,trace_complete=True)
    previous=ApplicationTraceEvent(0,ApplicationEventType.RUN_COMPLETED,now,None,base)
    event=ApplicationTraceEvent(0,ApplicationEventType.RUN_COMPLETED,now,None,
                                dict(base,memory=MemoryRunInfo().public_summary()))
    assert event.payload==previous.payload
    assert len(json.dumps(event.payload,ensure_ascii=False).encode())<=MAX_PUBLIC_PAYLOAD_BYTES
