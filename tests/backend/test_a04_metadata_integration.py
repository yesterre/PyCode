from dataclasses import replace
from uuid import UUID

import pytest
from sqlalchemy import update

from backend.app.core.errors import BackendError
from backend.app.db.models import AgentRunORM
from test_b01_users import uow_factory
from test_b02_auth import auth_context
from test_b04_project_derived_auth import resources
from test_b07_worker_isolation import workers,ready
from test_postgresql import postgresql_engine


def test_old_sync_and_agent_paths_persist_metadata_without_new_entrypoint(resources,workers,monkeypatch):
    r=resources;ready(r,workers);client=r.context.client
    for operation,body in [('ask',{'question':'main'}),('impact',{'file_path':'main.py'})]:
        response=client.post(f'/api/v1/projects/{r.own}/{operation}',headers=r.headers,json=body)
        assert response.status_code==200 and 'answer' in response.json()
        with r.context.uow_factory() as uow,uow.transaction():
            run=next(x for x in uow.agent_runs.list_for_project(r.own) if x.run_type==operation and x.request_metadata)
            assert run.result==response.json()['answer']
            assert run.execution_metadata['strategy']=='fast' and run.result_metadata['outcome']=='completed'
            assert run.request_metadata['entrypoint']==operation
        public=client.get(f'/api/v1/agent-runs/{run.id}',headers=r.headers).json()
        assert public['result']==run.result and public['result_metadata']==run.result_metadata
        assert public['result_metadata']['binding']['snapshot_id']==str(run.snapshot_id)
    observed=[];execute=r.context.app.state.adapter.run_agent
    def capture(*args,**kwargs):observed.append(kwargs.get('retrieval_context'));return execute(*args,**kwargs)
    monkeypatch.setattr(r.context.app.state.adapter,'run_agent',capture)
    submission=client.post('/api/v1/agent-runs',headers={**r.headers,'Idempotency-Key':'a04-agent'},
                           json={'project_id':str(r.own),'task':'explain main.py'})
    assert submission.status_code==202
    task=submission.json();workers.agent(task['task_id']);workers.agent(task['task_id'])
    assert len(observed)==1
    response=client.get('/api/v1/agent-runs/'+task['run_id'],headers=r.headers)
    body=response.json();assert body['status']=='completed'
    assert body['execution_metadata']['strategy']=='agent' and body['result_metadata']['outcome']=='completed'
    replay=client.post('/api/v1/agent-runs',headers={**r.headers,'Idempotency-Key':'a04-agent'},
                      json={'project_id':str(r.own),'task':'explain main.py'})
    assert replay.status_code==202
    assert replay.json()=={**task,'status':'completed'}
    trace=client.get('/api/v1/agent-runs/'+task['run_id']+'/trace',headers=r.headers).json()
    assert trace['events'] and all(e['schema_version']==1 for e in trace['events'])
    assert any(e['event_type']=='plan_created' for e in trace['events'])  # Still the real Runtime.
    assert client.get('/api/v1/agent-runs/'+task['run_id'],headers=r.other_headers).status_code==404
    assert not any(p.endswith('/questions') for p in client.get('/openapi.json').json()['paths'])
    injection=client.post('/api/v1/agent-runs',headers=r.headers,
        json={'project_id':str(r.own),'task':'attempt override','execution_metadata':{'strategy':'fast'}})
    assert injection.status_code==422


def test_failure_contract_and_unknown_stored_version_preserve_old_text_and_authorization(resources,workers,monkeypatch):
    r=resources;ready(r,workers);state=r.context.app.state;client=r.context.client
    def fail(*args,**kwargs):raise BackendError('model_timeout','safe timeout')
    monkeypatch.setattr(state.adapter,'ask',fail)
    response=client.post(f'/api/v1/projects/{r.own}/ask',headers=r.headers,json={'question':'main'})
    assert response.status_code==504
    with r.context.uow_factory() as uow,uow.transaction():
        run=next(x for x in uow.agent_runs.list_for_project(r.own) if x.request_metadata)
    body=client.get(f'/api/v1/agent-runs/{run.id}',headers=r.headers).json()
    assert body['status']=='failed' and body['result'] is None
    assert body['result_metadata']['outcome']=='failed' and body['result_metadata']['references']==[]
    assert 'execution_failed' in body['result_metadata']['diagnostics']
    bad={'schema_version':99,'prompt':'NEVER EXPOSE THIS'}
    with r.context.uow_factory() as uow,uow.transaction():
        if getattr(uow,'session',None) is not None:
            uow.session.execute(update(AgentRunORM).where(AgentRunORM.id==run.id).values(result_metadata=bad))
        else:uow.state.agent_runs[run.id]=replace(uow.state.agent_runs[run.id],result_metadata=bad)
    public=client.get(f'/api/v1/agent-runs/{run.id}',headers=r.headers)
    assert public.status_code==200 and 'NEVER EXPOSE THIS' not in public.text
    assert public.json()['result_metadata'] is None and public.json()['metadata_warnings']==['run_metadata_unavailable']
    assert client.get(f'/api/v1/agent-runs/{run.id}',headers=r.other_headers).status_code==404
    with r.context.uow_factory() as uow,uow.transaction():assert uow.agent_runs.get(run.id).result_metadata==bad


def test_unknown_queued_metadata_fails_before_core_and_preserves_raw_records(resources,workers,monkeypatch):
    r=resources;ready(r,workers);client=r.context.client
    accepted=client.post('/api/v1/agent-runs',headers=r.headers,json={'project_id':str(r.own),'task':'do not execute'})
    ids=accepted.json();run_id=UUID(ids['run_id']);raw={'schema_version':99,'private':'do not expose'}
    with r.context.uow_factory() as uow,uow.transaction():
        if getattr(uow,'session',None) is not None:
            uow.session.execute(update(AgentRunORM).where(AgentRunORM.id==run_id).values(request_metadata=raw))
        else:uow.state.agent_runs[run_id]=replace(uow.state.agent_runs[run_id],request_metadata=raw)
    def forbidden(*args,**kwargs):pytest.fail('Unknown metadata reached Core or workspace')
    monkeypatch.setattr(r.context.app.state.adapter,'run_agent',forbidden)
    monkeypatch.setattr(r.context.app.state.agent_workspace,'prepare',forbidden)
    with pytest.raises(BackendError) as error:workers.agent(ids['task_id'])
    assert error.value.code=='invalid_run_metadata'
    with r.context.uow_factory() as uow,uow.transaction():
        run=uow.agent_runs.get(run_id)
        assert run.status=='failed' and run.core_started_at is None and run.request_metadata==raw
        assert uow.background_tasks.get(UUID(ids['task_id'])).status=='failed'
    public=client.get('/api/v1/agent-runs/'+ids['run_id'],headers=r.headers)
    assert public.status_code==200 and 'do not expose' not in public.text
    assert public.json()['request_metadata'] is None and public.json()['metadata_warnings']
