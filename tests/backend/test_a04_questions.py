from uuid import UUID

import pytest

from backend.app.core.errors import BackendError
from test_b01_users import uow_factory
from test_b02_auth import auth_context
from test_b04_project_derived_auth import resources
from test_b07_worker_isolation import workers, ready
from test_postgresql import postgresql_engine


def post(r, text='explain main.py', key=None, **body):
    return r.context.client.post(f'/api/v1/projects/{r.own}/questions',
        headers={**r.headers, **({'Idempotency-Key': key} if key else {})}, json=dict(question=text, **body))


@pytest.mark.parametrize('text,strategy', [('explain main.py','fast'),
    ('impact of changing main.py','fast'), ('investigate cross-module call chain','agent')])
def test_question_full_worker_uses_saved_strategy_and_real_trace(resources, workers, monkeypatch, text, strategy):
    r=resources; ready(r,workers); state=r.context.app.state
    seen=[]
    method='answer_question' if strategy=='fast' else 'run_agent'
    original=getattr(state.adapter,method)
    def capture(*args,**kwargs):
        seen.append(kwargs['retrieval_context'].binding)
        return original(*args,**kwargs)
    monkeypatch.setattr(state.adapter,method,capture)
    response=post(r,text,'question-once')
    assert response.status_code==202, response.text
    data=response.json(); workers.agent(data['task_id']); workers.agent(data['task_id'])
    run=r.context.client.get('/api/v1/agent-runs/'+data['run_id'],headers=r.headers).json()
    assert run['status']=='completed', run
    assert len(seen)==1 and str(seen[0].snapshot_id)==run['snapshot_id']
    assert run['execution_metadata']['schema_version']==(3 if strategy=='fast' else 2)
    assert run['execution_metadata']['strategy']==strategy
    assert run['result_metadata']['outcome']=='completed'
    assert run['result_metadata']['binding']==run['execution_metadata']['binding']
    events=r.context.client.get('/api/v1/agent-runs/'+data['run_id']+'/trace',headers=r.headers).json()['events']
    kinds=[e['event_type'] for e in events]
    assert kinds[0]=='run_started' and kinds[-1]=='run_completed'
    if strategy=='fast':
        assert kinds==['run_started','context_updated','llm_started','llm_finished','run_completed']
        assert events[2]['payload']['purpose']=='answer'
        assert 'retrieval' in events[1]['payload']
        assert events[-1]['payload']['trace_complete'] is True
        assert run['result_metadata']['references']
    else: assert 'plan_created' in kinds
    from test_b06_sse_auth import StreamRedis
    state.realtime_redis=StreamRedis([event for rid,event in workers.published if str(rid)==data['run_id']])
    stream=r.context.client.get('/api/v1/agent-runs/'+data['run_id']+'/events',
        headers={**r.headers,'Last-Event-ID':'0'})
    assert stream.status_code==200 and 'id: 0\n' not in stream.text and 'event: trace' in stream.text
    assert 'run_completed' in stream.text
    assert r.context.client.get('/api/v1/agent-runs/'+data['run_id']+'/events',headers=r.other_headers).status_code==404
    replay=post(r,text,'question-once')
    assert replay.json()=={**data,'status':'completed'}
    assert post(r,text+' changed','question-once').status_code==409
    old=r.context.client.post('/api/v1/agent-runs',headers={**r.headers,'Idempotency-Key':'question-once'},
        json={'project_id':str(r.own),'task':text})
    assert old.status_code==409
    history=r.context.client.get(f'/api/v1/projects/{r.own}/analysis-history',headers=r.headers).json()
    assert any(row['id']==data['run_id'] and row['answer']==run['result'] for row in history['items'])


def test_question_authorization_ready_and_parameter_guards_precede_any_expensive_work(resources,workers,monkeypatch):
    r=resources; ready(r,workers); client=r.context.client
    def forbidden(*args,**kwargs):pytest.fail('Unauthorized input reached Artifact or Provider')
    monkeypatch.setattr(r.context.app.state.artifacts,'validate',forbidden)
    monkeypatch.setattr(r.context.app.state.adapter,'answer_question',forbidden)
    url=f'/api/v1/projects/{r.own}/questions'
    assert client.post(url,json={'question':'main.py'}).status_code==401
    assert client.post(url,headers=r.other_headers,json={'question':'main.py'}).status_code==404
    assert post(r,snapshot_id=str(r.foreign_snapshot.id)).status_code==404
    assert post(r,strategy='fast').status_code==422
    assert post(r,commit_sha='f'*40).status_code==422
    assert client.post(f'/api/v1/projects/{r.own}/archive',headers=r.headers).status_code==200
    assert post(r).status_code==409


def test_fast_failure_preserves_real_events_and_no_redelivery(resources,workers,monkeypatch):
    r=resources;ready(r,workers)
    def fail(*args,**kwargs):raise BackendError('model_timeout','Model timed out.')
    # Fail at the LLM boundary, after actual retrieval and before answer completion.
    monkeypatch.setattr(r.context.app.state.adapter.llm_client,'generate',fail)
    data=post(r).json()
    with pytest.raises(BackendError):workers.agent(data['task_id'])
    workers.agent(data['task_id'])
    run=r.context.client.get('/api/v1/agent-runs/'+data['run_id'],headers=r.headers).json()
    assert run['status']=='failed' and run['core_started_at'] is not None
    assert run['result_metadata']['outcome']=='failed'
    events=r.context.client.get('/api/v1/agent-runs/'+data['run_id']+'/trace',headers=r.headers).json()['events']
    assert [e['event_type'] for e in events]==['run_started','context_updated','llm_started','llm_finished','run_failed']
    assert run['result_metadata']['quality']=='partial' and run['result_metadata']['references']
