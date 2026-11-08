"""Trusted Memory through real authorized services, Worker and both UoWs."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from uuid import UUID, uuid4

import pytest

from backend.app.config import StaleReconciliationSettings
from backend.app.services.stale_reconciliation import StaleRunningReconciler
from backend.app.tasks import agent_runs
from pycode.agent.budget import current_budget
from pycode.agent.memory import MemoryStore
from pycode.agent.memory_record import MemoryScope, provenance_for
from pycode.chunks import SnapshotBinding
from test_a01b_semantics import semantic_env
from test_a02b_backend_vector_retrieval import vector_env
from test_a04_question_lifecycle import submit, service, switch_current, expire
from test_b01_users import uow_factory
from test_b02_auth import auth_context
from test_b04_project_derived_auth import resources
from test_b07_worker_isolation import workers, accept_index
from test_b05_async_auth import submit as submit_legacy
from test_postgresql import postgresql_engine


class MemoryModel:
    def __init__(self, env, ids):
        self.env, self.ids, self.prompts = env, ids, []

    def generate(self, prompt):
        env = self.env; budget = current_budget()
        assert not env.active_transactions and not env.memory_transactions
        assert budget.binding.snapshot_id == str(env.snapshot_id)
        assert 'OLD_SNAPSHOT_PRIVATE' not in prompt and 'LEGACY_PRIVATE' not in prompt
        self.prompts.append(prompt)
        if prompt.startswith('You are the PyCode LLM Planner.'):
            return json.dumps([dict(tool='memory',arguments=dict(operation='add',id='tool-note',
                memory_type='analysis',body='PRIVATE_MEMORY_BODY'),required=False)])
        if prompt.startswith('You are the PyCode LLM Next-Action Planner.'):
            payload=json.loads(prompt.split('Next-action input:\n')[-1])
            if payload['turn']['turn_index']==1:
                return json.dumps(dict(action_type='tool_call',tool_name='memory',arguments=dict(
                    operation='add',id='tool-note',memory_type='analysis',body='PRIVATE_MEMORY_BODY')))
            return json.dumps(dict(action_type='final_answer',final_answer='Analysis succeeded; Memory remains a hint.'))
        if prompt.startswith('Extract durable PyCode memories'):
            return json.dumps([dict(id='auto-note',type='preference',body='PRIVATE_AUTO_MEMORY_BODY',
                origin='user_explicit',project_id=str(uuid4()),run_id=str(uuid4()))])
        return '[]'


@pytest.mark.parametrize('mode',['normal','recover','archived','corrupt','write_failure'])
def test_real_worker_memory_trusted_original_scope_recovery_and_safe_trace(vector_env, monkeypatch, mode):
    env=vector_env; ids=submit(env,'agent')
    root=env.app.state.workspace.path_for(env.r.own)
    original_binding=SnapshotBinding(str(env.r.own),str(env.snapshot_id),'a'*40)
    old_scope=MemoryScope(root,replace(original_binding,snapshot_id=str(uuid4())),str(uuid4()))
    old_store=MemoryStore(root,scope=old_scope)
    old_store.add_memory(memory_id='old',memory_type='analysis',title='Old',summary='Old',body='OLD_SNAPSHOT_PRIVATE')
    legacy=old_store.memory_dir/'legacy.md'
    legacy.write_text('---\nid: "legacy"\ntype: "project"\n---\nLEGACY_PRIVATE',encoding='utf-8')
    preserved={p.name:p.read_bytes() for p in old_store.memory_dir.glob('*.md') if p.name!='MEMORY.md'}
    if mode=='corrupt': (old_store.memory_dir/'broken.md').write_bytes(b'\xff')
    if mode=='recover':
        with env.r.context.uow_factory() as uow,uow.transaction():
            task=uow.background_tasks.claim(ids['task_id']).task
            uow.agent_runs.mark_running(ids['run_id'])
        expire(env,task)
        with env.r.context.uow_factory() as uow:
            result=StaleRunningReconciler(uow,env.app.state.project_operations,StaleReconciliationSettings()).reconcile()
        assert result.requeued==1
    newer=switch_current(env)
    if mode=='archived':
        response=env.r.context.client.post(f'/api/v1/projects/{env.r.own}/archive',headers=env.r.headers)
        assert response.status_code==200,response.text
    model=MemoryModel(env,ids)
    monkeypatch.setattr(env.app.state.adapter.llm_client,'generate',model.generate)
    writes=[]; save=MemoryStore._save_memory
    def guarded_save(store,item):
        assert not env.active_transactions and not env.memory_transactions
        expected=MemoryScope(root,original_binding,str(ids['run_id']))
        assert item.provenance==provenance_for(expected,'model_inference')
        writes.append(item.id)
        if mode=='write_failure': raise OSError('PRIVATE_MEMORY_IO_FAILURE')
        return save(store,item)
    monkeypatch.setattr(MemoryStore,'_save_memory',guarded_save)
    @contextmanager
    def services():
        with env.r.context.uow_factory() as uow: yield service(env,uow)
    monkeypatch.setattr(agent_runs,'worker_agent_run_service',services)
    result=agent_runs.run_agent_task.run(str(ids['task_id']))
    with env.r.context.uow_factory() as uow,uow.transaction():
        run=uow.agent_runs.get(ids['run_id'])
        assert uow.snapshots.get(env.snapshot_id).status=='ready'
        assert uow.projects.get(env.r.own).current_snapshot_id==newer.id
    assert run.status=='completed' and run.snapshot_id==env.snapshot_id!=newer.id
    calls=len(model.prompts)
    assert agent_runs.run_agent_task.run(str(ids['task_id']))==result
    assert len(model.prompts)==calls and writes==['tool-note','auto-note']
    assert {p.name:p.read_bytes() for p in old_store.memory_dir.glob('*.md') if p.name in preserved}==preserved
    with MemoryScope(root,original_binding,str(ids['run_id'])).activate():
        current=MemoryStore(root)
        assert {e.id for e in current.list_memories()}== (set() if mode=='write_failure' else {'tool-note','auto-note'})
        if mode!='write_failure':
            for entry in current.list_memories():
                assert current.load_memory(entry.id).provenance==provenance_for(current.scope,'model_inference')
    response=env.r.context.client.get(f'/api/v1/agent-runs/{run.id}/trace',headers=env.r.headers)
    assert response.status_code==200
    public=json.dumps(response.json())
    assert all(secret not in public for secret in ('PRIVATE_MEMORY_BODY','PRIVATE_AUTO_MEMORY_BODY','PRIVATE_MEMORY_IO_FAILURE','OLD_SNAPSHOT_PRIVATE','LEGACY_PRIVATE'))
    assert response.json()['events'][-1]['event_type']=='run_completed'
    assert env.r.context.client.get(f'/api/v1/agent-runs/{run.id}/trace',headers=env.r.other_headers).status_code==404
    if mode=='archived':
        assert env.r.context.client.get(f'/api/v1/projects/{env.r.own}',headers=env.r.headers).json()['archived_at'] is not None


def test_unauthorized_submission_never_enters_memory_or_model_scope(vector_env,monkeypatch):
    env=vector_env
    def forbidden(*a,**kw): pytest.fail('Unauthorized request accessed Memory/Runtime')
    monkeypatch.setattr(env.app.state.adapter,'execution_scope',forbidden)
    monkeypatch.setattr(MemoryStore,'__init__',forbidden)
    monkeypatch.setattr(env.app.state.adapter.llm_client,'generate',forbidden)
    response=env.r.context.client.post(f'/api/v1/projects/{env.r.own}/questions',headers=env.r.other_headers,
        json={'question':'cross-module call chain'})
    assert response.status_code==404 and not env.created


def test_two_user_workers_cannot_mix_memory_even_at_same_commit(resources,workers,monkeypatch):
    r=resources; context=r.context; state=context.app.state
    pairs=[(r.own,r.headers),(r.foreign,r.other_headers)]
    for project,headers in pairs:
        workers.index(accept_index(context,project,headers))
    submitted=[]
    for project,headers in pairs:
        response=submit_legacy(context,project,headers,task='explain the entry point')
        assert response.status_code==202,response.text
        submitted.append(response.json())
    def generate(prompt):
        project=current_budget().binding.project_id
        if prompt.startswith('You are the PyCode LLM Planner.'):
            return json.dumps([dict(tool='memory',arguments=dict(operation='add',id='same-name',
                memory_type='analysis',body='Private '+project),required=False)])
        if prompt.startswith('You are the PyCode LLM Next-Action Planner.'):
            turn=json.loads(prompt.split('Next-action input:\n')[-1])['turn']['turn_index']
            if turn==1:return json.dumps(dict(action_type='tool_call',tool_name='memory',arguments=dict(
                operation='add',id='same-name',memory_type='analysis',body='Private '+project)))
            return json.dumps(dict(action_type='final_answer',final_answer='Analysis completed'))
        return '[]'
    monkeypatch.setattr(state.adapter.llm_client,'generate',generate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(workers.agent,[item['task_id'] for item in submitted]))
    for i,((project,headers),data) in enumerate(zip(pairs,submitted)):
        root=state.workspace.path_for(project)
        item=MemoryStore(root).load_memory('same-name')
        assert item.body=='Private '+str(project)
        assert item.provenance['project_id']==str(project) and item.provenance['run_id']==data['run_id']
        with context.uow_factory() as uow,uow.transaction():
            run=uow.agent_runs.get(UUID(data['run_id']))
            assert run.status=='completed'
            assert item.provenance['snapshot_id']==str(run.snapshot_id)
            snap=uow.snapshots.get(run.snapshot_id)
            assert item.provenance['commit_sha']==snap.commit_sha=='a'*40
        assert context.client.get(f'/api/v1/agent-runs/{run.id}/trace',headers=pairs[1-i][1]).status_code==404
