import json
from uuid import UUID, uuid4

import pytest

from backend.app.core.errors import BackendError
from backend.app.config import RetrievalSettings
from backend.app.db.models import BackgroundTaskORM
from pycode.agent.budget import current_budget
from test_b01_users import uow_factory
from test_b02_auth import auth_context
from test_b04_project_derived_auth import resources
from test_b07_worker_isolation import workers, ready, mutate
from test_postgresql import postgresql_engine
from test_a01b_semantics import semantic_env
from test_a02b_backend_vector_retrieval import vector_env
from test_a04_question_lifecycle import submit, service, switch_current


def post(r, text='cross-module call chain', **body):
    return r.context.client.post(f'/api/v1/projects/{r.own}/questions', headers=r.headers,
                                  json=dict(question=text, **body))


def trace(r, run_id):
    return r.context.client.get(f'/api/v1/agent-runs/{run_id}/trace', headers=r.headers).json()['events']


@pytest.mark.parametrize('variable,reason,models', [
    ('PYCODE_AGENT_MAX_MODEL_CALLS', 'budget_models', 0),
    ('PYCODE_AGENT_MAX_TOOL_CALLS', 'budget_tools', 2),
])
def test_real_worker_budget_failure_is_durable_and_redelivery_never_reenters(
        resources, workers, monkeypatch, variable, reason, models):
    r = resources
    ready(r, workers)
    app = r.context.app
    monkeypatch.setenv(variable, '0')
    ids = post(r).json()
    with pytest.raises(BackendError):
        workers.agent(ids['task_id'])
    before = len(app.state.adapter.llm_client.prompts)
    workers.agent(ids['task_id'])
    assert len(app.state.adapter.llm_client.prompts) == before == models
    run = r.context.client.get('/api/v1/agent-runs/' + ids['run_id'], headers=r.headers).json()
    assert run['status'] == 'failed' and run['core_started_at']
    assert run['result_metadata']['outcome'] == 'failed'
    events = trace(r, ids['run_id'])
    budget = events[-1]['payload']['harness_budget']
    assert events[-1]['event_type'] == 'run_failed' and budget['stop_reason'] == reason
    assert budget['usage']['model_calls'] == models
    assert budget['transport_attempts'] == 'not_observed'
    assert 'cross-module' not in json.dumps(budget)
    with r.context.uow_factory() as uow, uow.transaction():
        assert uow.snapshots.get(UUID(run['snapshot_id'])).status == 'ready'
    assert current_budget() is None


def test_fast_keeps_old_event_sequence_and_reports_actual_shared_model_budget(resources, workers, monkeypatch):
    r = resources
    ready(r, workers)
    monkeypatch.setenv('PYCODE_AGENT_MAX_MODEL_CALLS', '1')
    ids = post(r, 'explain main.py').json()
    workers.agent(ids['task_id'])
    events = trace(r, ids['run_id'])
    assert [e['event_type'] for e in events] == [
        'run_started', 'context_updated', 'llm_started', 'llm_finished', 'run_completed']
    budget = events[-1]['payload']['harness_budget']
    assert budget['usage'] == dict(turns=0,tool_calls=0,model_calls=1,query_embeddings=0)
    assert budget['stop_reason'] is None


def test_client_and_foreign_owner_cannot_increase_budget_or_enter_scope(resources, workers, monkeypatch):
    r = resources
    ready(r, workers)
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid or foreign request reached the trusted execution scope')
    monkeypatch.setattr(r.context.app.state.adapter, 'execution_scope', forbidden)
    assert post(r, max_model_calls=999).status_code == 422
    assert post(r, execution_limits={'max_turns':999}).status_code == 422
    denied = r.context.client.post(f'/api/v1/projects/{r.own}/questions', headers=r.other_headers,
        json={'question':'main.py'})
    assert denied.status_code == 404


def test_loss_during_first_model_cannot_call_another_provider_or_write_terminal(resources, workers, monkeypatch):
    r = resources
    ready(r, workers)
    ids = post(r).json()
    calls = []
    def revoke(prompt):
        calls.append(1)
        with r.context.uow_factory() as uow, uow.transaction():
            mutate(uow, 'background_tasks', BackgroundTaskORM, UUID(ids['task_id']), execution_token=uuid4())
        return json.dumps([dict(tool='retrieve_context',arguments={},required=True)])
    monkeypatch.setattr(r.context.app.state.adapter.llm_client, 'generate', revoke)
    workers.agent(ids['task_id'])
    assert calls == [1]
    with r.context.uow_factory() as uow, uow.transaction():
        run = uow.agent_runs.get(UUID(ids['run_id']))
        assert run.status == 'running' and run.core_started_at and run.result is None
        assert uow.trace_events.list_for_run(run.id) == []
    assert current_budget() is None


def test_upgrade_keeps_query_budget_snapshot_and_initial_pack_without_reembedding(vector_env, monkeypatch):
    env = vector_env
    monkeypatch.setenv('PYCODE_AGENT_MAX_QUERY_EMBEDDINGS', '1')
    ids = submit(env, 'fast')
    newer = switch_current(env)
    seen, usage = [], []
    def model(prompt):
        budget = current_budget()
        assert budget.binding.snapshot_id == str(env.snapshot_id)
        assert not env.active_transactions and not env.memory_transactions
        usage.append(dict(budget.usage))
        if prompt.startswith('You are the PyCode LLM Planner.'):
            payload = json.loads(prompt.split('Planning input:\n')[-1])
            seen.append(payload['initial_evidence'])
            return json.dumps([dict(tool='retrieve_context',arguments={'question':'return'},required=False)])
        if prompt.startswith('You are the PyCode LLM Next-Action Planner.'):
            turn = json.loads(prompt.split('Next-action input:\n')[-1])['turn']['turn_index']
            return json.dumps(dict(action_type='tool_call',tool_name='retrieve_context',
                arguments={'question':'return' if turn == 1 else 'new query'}))
        return '[]'
    monkeypatch.setattr(env.app.state.adapter.llm_client, 'generate', model)
    with env.r.context.uow_factory() as uow:
        worker = service(env, uow)
        worker.projects.semantics.retrieval_settings_factory = lambda: RetrievalSettings('vector',token_budget=4096)
        with pytest.raises(BackendError):
            worker.execute(ids['task_id'])
        run = worker.get(ids['run_id'])
        assert worker.execute(ids['task_id']) == run
    assert run.status == 'failed' and run.snapshot_id == env.snapshot_id != newer.id
    assert run.execution_metadata['strategy'] == 'agent' and len(seen) == 1
    assert usage and all(item['query_embeddings'] == 1 for item in usage)
    assert len(env.created) == 1 and env.created[0].closed and len(env.created[0].queries) == 1
    assert run.result_metadata['references']
    events = trace(env.r, run.id)
    budget = events[-1]['payload']['harness_budget']
    assert budget['stop_reason'] == 'budget_embeddings' and budget['usage']['query_embeddings'] == 1
    reused = [e for e in events if e['payload'].get('retrieval_origin') == 'question_preflight']
    assert len(reused) == 1 and reused[0]['payload']['initial_pack_digest'] == seen[0]['initial_pack_digest']
