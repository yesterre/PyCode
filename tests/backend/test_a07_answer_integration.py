"""New A07b Fake policy through existing offline HTTP/Worker fixtures."""
import json
from uuid import UUID

import pytest

from scripts.benchmarks.a07_answer_case import ScriptedModel


@pytest.mark.parametrize('question,strategy',[
    ('explain main.py','fast'),('investigate cross-module serve call chain','agent')])
def test_evaluation_fake_uses_real_admission_worker_and_safe_trace(client,ready_url,application,agent_worker,question,strategy):
    model=ScriptedModel(question)
    application.state.adapter.llm_client=model
    # Admission accepts no evaluator-provided trusted routing controls.
    denied=client.post(ready_url+'/questions',json={'question':question,'strategy':'fast'})
    assert denied.status_code==422
    accepted=client.post(ready_url+'/questions',json={'question':question})
    assert accepted.status_code==202,accepted.text
    run=accepted.json();worker=agent_worker
    worker.execute(UUID(run['task_id']))
    calls=len(model.calls)
    worker.execute(UUID(run['task_id']))
    assert len(model.calls)==calls  # No new model execution on duplicate delivery.
    response=client.get('/api/v1/agent-runs/'+run['run_id'])
    assert response.status_code==200
    value=response.json()
    assert value['status']=='completed',value
    assert value['execution_metadata']['strategy']==strategy
    assert value['result_metadata']['binding']==value['execution_metadata']['binding']
    events=client.get('/api/v1/agent-runs/'+run['run_id']+'/trace').json()['events']
    assert events[0]['event_type']=='run_started' and events[-1]['event_type']=='run_completed'
    encoded=json.dumps(events,ensure_ascii=False)
    assert 'from service import serve' not in encoded and 'return 42' not in encoded
    assert not any(key in event['payload'] for event in events for key in ('prompt','body','content','api_key'))
    assert model.calls and all(c['estimated_input_tokens']<=131072 for c in model.calls)
    if strategy=='agent':assert any(c['phase']=='next_action' for c in model.calls)
    else:assert len(model.calls)==1
