"""Production HTTP/Worker vector wiring with offline Providers and both UoWs."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import event, update
from sqlalchemy.orm import Session

from backend.app.config import EmbeddingSettings, RetrievalSettings, StaleReconciliationSettings
from backend.app.db.models import BackgroundTaskORM
from backend.app.core.application_trace import ApplicationTraceNormalizer, public_retrieval_summary
from backend.app.core.models import AgentRunStatus
from backend.app.core.realtime_trace import RealtimeTraceSession
from backend.app.repositories import InMemoryUnitOfWork
from backend.app.services.background_tasks import BackgroundTaskService
from backend.app.services.stale_reconciliation import StaleRunningReconciler
from pycode.agent.trace import ToolTrace
from pycode.embedding import EmbeddingResult, EmbeddingError
from pycode.semantic_search import VectorUnavailable
from pycode.tools import ToolContext, retrieve_context
from pycode.tools.base import success
from test_a01b_semantics import semantic_env, automatic
from test_b01_users import uow_factory
from test_b02_auth import auth_context
from test_b04_project_derived_auth import resources
from test_postgresql import postgresql_engine


@pytest.fixture
def vector_env(semantic_env, monkeypatch):
    env = semantic_env
    base, tasks = automatic(env)
    assert len(tasks) == 1
    assert env.run(tasks[0].id).status == 'completed'
    env.snapshot_id = base.snapshot_id
    env.created, env.active_transactions, env.memory_transactions = [], set(), []
    env.failure, env.bad_response = None, None
    env.app.state.retrieval_settings_factory = lambda: RetrievalSettings('vector')
    def started(session, transaction, connection):
        env.active_transactions.add(id(session))
    def ended(session, transaction):
        if transaction.parent is None:
            env.active_transactions.discard(id(session))
    event.listen(Session, 'after_begin', started)
    event.listen(Session, 'after_transaction_end', ended)
    original = InMemoryUnitOfWork.transaction
    @contextmanager
    def memory_transaction(self):
        env.memory_transactions.append(self)
        try:
            with original(self):
                yield
        finally:
            env.memory_transactions.pop()
    monkeypatch.setattr(InMemoryUnitOfWork, 'transaction', memory_transaction)
    class QueryProvider:
        provider = env.settings.provider
        model = env.settings.model
        dimension = 1024
        def __init__(self):
            self.closed, self.queries = False, []
        def embed_documents(self, texts):
            pytest.fail('Query path must not build an index')
        def embed_query(self, text):
            assert not env.active_transactions and not env.memory_transactions
            self.queries.append(text)
            if env.failure:
                raise env.failure
            result = EmbeddingResult(self.provider, self.model, self.dimension, 1, [[1.0] * self.dimension])
            return env.bad_response(result) if env.bad_response else result
        def close(self):
            self.closed = True
    def factory(settings):
        assert settings == env.settings
        provider = QueryProvider()
        env.created.append(provider)
        return provider
    env.app.state.embedding_provider_factory = factory
    env.factory = factory
    env.settings_factory = lambda: env.settings
    env.ask = lambda question='return': env.r.context.client.post(
        f'/api/v1/projects/{env.r.own}/ask', headers=env.r.headers, json={'question': question})
    try:
        yield env
    finally:
        event.remove(Session, 'after_begin', started)
        event.remove(Session, 'after_transaction_end', ended)


def snapshot(env):
    with env.r.context.uow_factory() as uow, uow.transaction():
        return uow.snapshots.get(env.snapshot_id)


def test_http_vector_and_tool_share_exact_published_evidence(vector_env, monkeypatch):
    env = vector_env
    snap = snapshot(env)
    with env.r.context.uow_factory() as uow:
        target, _ = env.projects(uow).semantics.target(snap, env.settings)
    directory = env.app.state.artifacts.semantic_directory(target) / 'ready'
    def files():
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()}
    before = files()
    def forbidden(*args, **kwargs):
        pytest.fail('Query must never recover, build or publish an Artifact')
    for method in ('recover_semantic', 'publish_semantic', 'build_and_publish'):
        monkeypatch.setattr(env.app.state.artifacts, method, forbidden)
    observed = []
    original = env.app.state.adapter.engine.ask
    def ask(*args, **kwargs):
        observed.append(kwargs['retrieval_context'])
        return original(*args, **kwargs)
    monkeypatch.setattr(env.app.state.adapter.engine, 'ask', ask)
    response = env.ask()
    assert response.status_code == 200, response.text
    summary = response.json()['retrieval']
    assert summary['status'] == 'ok'
    assert summary['references'] and all(r['source'] == 'vector' for r in summary['references'])
    assert summary['binding']['snapshot_id'] == str(env.snapshot_id)
    context = observed[0]
    tool = retrieve_context(ToolContext(context.root, retrieval_context=context), 'return')
    assert tool.ok
    assert tool.data['retrieval']['references'] == summary['references']
    assert tool.data['evidence'] == response.json()['evidence']
    assert all(p.closed and p.queries == ['return'] for p in env.created)
    assert files() == before


@pytest.mark.parametrize('failure', ['missing', 'corrupt', 'disabled', 'timeout', 'exception', 'dimension', 'model', 'zero'])
def test_channel_failures_close_provider_and_preserve_ready_snapshot(vector_env, failure):
    env = vector_env
    snap = snapshot(env)
    with env.r.context.uow_factory() as uow:
        target, chunks = env.projects(uow).semantics.target(snap, env.settings)
    directory = env.app.state.artifacts.semantic_directory(target) / 'ready'
    if failure == 'missing':
        (directory / 'manifest.json').rename(directory / 'saved-manifest.json')
    elif failure == 'corrupt':
        (directory / 'vectors.f32').write_bytes(b'bad')
    elif failure == 'disabled':
        env.settings = EmbeddingSettings()
    elif failure == 'timeout':
        env.failure = EmbeddingError('embedding_timeout')
    elif failure == 'exception':
        env.failure = RuntimeError('PRIVATE_QUERY_AND_CREDENTIAL')
    else:
        env.bad_response = lambda r: replace(r, **{
            'dimension': {'dimension': 3}, 'model': {'model': 'wrong'}, 'zero': {'vectors': [[0.0] * 1024]}
        }[failure])
    result = env.ask()
    assert result.status_code == 200, result.text
    summary = result.json()['retrieval']
    assert summary['status'] == 'degraded'
    assert any(d.startswith('vector_') for d in summary['diagnostics'])
    assert all(r['source'] == 'keyword' for r in summary['references'])
    assert snapshot(env).status == 'ready'
    assert all(p.closed for p in env.created)
    if failure in {'missing', 'corrupt', 'disabled'}:
        assert env.created == []
    assert 'PRIVATE_QUERY_AND_CREDENTIAL' not in result.text


def test_exact_and_impact_and_denied_requests_do_not_access_vector(vector_env, monkeypatch):
    env = vector_env
    def forbidden(*args, **kwargs):
        pytest.fail('This path must not read vector artifacts or create a Provider')
    monkeypatch.setattr(env.app.state.artifacts, 'read_vector_index', forbidden)
    env.app.state.embedding_provider_factory = forbidden
    assert env.ask('serve').status_code == 200
    assert env.r.context.client.post(f'/api/v1/projects/{env.r.own}/impact', headers=env.r.headers,
                                   json={'file_path': 'service.py'}).status_code == 200
    assert env.r.context.client.post(f'/api/v1/projects/{env.r.own}/ask', headers=env.r.other_headers,
                                   json={'question': 'return'}).status_code == 404
    assert env.r.context.client.post('/api/v1/agent-runs', headers=env.r.other_headers,
                                   json={'project_id': str(env.r.own), 'task': 'return'}).status_code == 404
    assert not env.created


def test_trace_live_and_final_keep_vector_refs_without_code_or_credentials(vector_env):
    env = vector_env
    observed = env.ask()
    assert observed.status_code == 200
    summary = observed.json()['retrieval']
    malicious = {**summary, 'query': 'PRIVATE_QUERY', 'api_key': 'SECRET', 'vectors': [[1]], 'snippet': 'SOURCE'}
    safe = public_retrieval_summary(malicious)
    assert safe and any(r['source'] == 'vector' for r in safe['references'])
    assert public_retrieval_summary(safe) == safe
    assert all(value not in json.dumps(safe) for value in ['PRIVATE_QUERY', 'SECRET', 'SOURCE', 'vectors', 'api_key'])
    trace = ToolTrace(1, 'retrieve_context', datetime.now(timezone.utc))
    trace.finish(success('retrieve_context', 'Vector result', retrieval=malicious))
    final = ApplicationTraceNormalizer._tool_finished(trace)[3]['retrieval']
    with env.r.context.uow_factory() as uow, uow.transaction():
        run = uow.agent_runs.create(env.r.own, env.snapshot_id, task='return', run_type='agent', model=None)
    live = RealtimeTraceSession(replace(run, started_at=datetime.now(timezone.utc)), SimpleNamespace(ensure_owned=lambda: None), None)
    live.observe('tool_finished', trace)
    assert live.events[-1].payload['retrieval'] == final == safe


@pytest.mark.parametrize('recover_before_core', [False, True])
def test_worker_original_snapshot_vector_factory_and_redelivery(vector_env, monkeypatch, recover_before_core):
    env = vector_env
    accepted = env.r.context.client.post('/api/v1/agent-runs', headers=env.r.headers,
                                        json={'project_id': str(env.r.own), 'task': 'return'})
    assert accepted.status_code == 202, accepted.text
    task_id = UUID(accepted.json()['task_id'])
    if recover_before_core:
        with env.r.context.uow_factory() as uow:
            with uow.transaction():
                task = uow.background_tasks.claim(task_id).task
                uow.agent_runs.mark_running(task.agent_run_id)
                expired = datetime.now(timezone.utc) - timedelta(minutes=10)
                if hasattr(uow, 'session'):
                    uow.session.execute(update(BackgroundTaskORM).where(BackgroundTaskORM.id == task_id).values(lease_expires_at=expired))
                else:
                    uow.state.background_tasks[task_id] = replace(task, lease_expires_at=expired)
            result = StaleRunningReconciler(uow, env.app.state.project_operations, StaleReconciliationSettings()).reconcile()
            assert result.requeued == 1
        assert not env.created  # recovery itself cannot query a model
    root = env.app.state.workspace.path_for(env.r.own)
    # Change current_snapshot after submission; the worker must still query old vectors.
    with env.r.context.uow_factory() as uow:
        projects = env.projects(uow)
        with uow.transaction():
            newer = uow.snapshots.start(env.r.own, 'b' * 40)
        bundle = env.app.state.artifacts.build_and_publish(env.r.own, newer.id, newer.commit_sha,
            lambda i, g: env.app.state.adapter.index(root, index_path=i, graph_path=g,
                project_id=env.r.own, snapshot_id=newer.id, commit_sha=newer.commit_sha), require_chunks=True)
        with uow.transaction():
            uow.snapshots.mark_ready(newer.id, index_artifact_path=bundle.index_artifact_path,
                                    graph_artifact_path=bundle.graph_artifact_path, summary=bundle.summary)
            uow.projects.set_current_snapshot(env.r.own, newer.id)
    from backend.app.tasks import runtime
    observed, published = [], []
    original = env.app.state.adapter.engine.run_agent
    def run(*args, **kwargs):
        assert kwargs['retrieval_context'].binding.snapshot_id == str(env.snapshot_id)
        result = original(*args, **kwargs)
        observed.append(result)
        return result
    monkeypatch.setattr(env.app.state.adapter.engine, 'run_agent', run)
    for name, value in {
        '_adapter': env.app.state.adapter, '_workspace': env.app.state.workspace,
        '_artifacts': env.app.state.artifacts, '_project_operations': env.app.state.project_operations,
        '_agent_workspace': env.app.state.agent_workspace, '_admission': None,
        '_embedding_settings_factory': env.settings_factory, '_embedding_provider_factory': env.factory,
        '_retrieval_settings_factory': lambda: RetrievalSettings('vector'),
        '_trace_publisher': SimpleNamespace(publish=lambda run_id, event: published.append(event)),
        '_background_task_service': lambda uow: BackgroundTaskService(uow),
    }.items():
        monkeypatch.setattr(runtime, name, value)
    with env.r.context.uow_factory() as uow:
        session = getattr(uow, 'session', SimpleNamespace(in_transaction=lambda: False, close=lambda: None))
        monkeypatch.setattr(runtime, '_database', SimpleNamespace(open_session=lambda: session))
        monkeypatch.setattr(runtime, 'SqlAlchemyUnitOfWork', lambda session: uow)
        with runtime.worker_agent_run_service() as service:
            completed = service.execute(task_id)
            assert completed.status == AgentRunStatus.COMPLETED
            assert service.execute(task_id).id == completed.id
    assert len(observed) == 1 and env.created and all(p.closed for p in env.created)
    tools = [r for r in observed[0].tool_results if r.tool == 'retrieve_context']
    assert tools and all(r['source'] == 'vector' for r in tools[0].data['items'])
    summaries = [e.payload['retrieval'] for e in published if 'retrieval' in e.payload]
    assert summaries and all(s['binding']['snapshot_id'] == str(env.snapshot_id) for s in summaries)


def test_foreign_manifest_at_correct_path_is_rejected_without_provider(vector_env):
    env = vector_env
    snap = snapshot(env)
    with env.r.context.uow_factory() as uow:
        target, _ = env.projects(uow).semantics.target(snap, env.settings)
    path = env.app.state.artifacts.semantic_directory(target) / 'ready' / 'manifest.json'
    manifest = json.loads(path.read_text(encoding='utf-8'))
    # A different Project uses the exact same Commit in this fixture.
    manifest['identity']['binding']['project_id'] = str(env.r.foreign)
    assert manifest['identity']['binding']['commit_sha'] == snap.commit_sha
    path.write_text(json.dumps(manifest), encoding='utf-8')
    result = env.ask()
    assert result.status_code == 200
    assert 'vector_artifact_unavailable' in result.json()['retrieval']['diagnostics']
    assert not env.created and snapshot(env).status == 'ready'


def test_query_input_bound_and_config_bound_are_explicit(vector_env):
    env = vector_env
    result = env.ask('x' * (env.settings.max_input_chars + 1))
    assert result.status_code == 200
    assert 'vector_query_too_long' in result.json()['retrieval']['diagnostics']
    assert not env.created
    def invalid():
        raise ValueError('private invalid config')
    env.app.state.retrieval_settings_factory = invalid
    result = env.ask()
    assert result.status_code == 200
    assert 'vector_configuration' in result.json()['retrieval']['diagnostics']
    assert 'private invalid config' not in result.text


def test_query_refuses_accidental_sqlalchemy_transaction(vector_env):
    env = vector_env
    snap = snapshot(env)
    with env.r.context.uow_factory() as uow:
        if not hasattr(uow, 'session'):
            return  # Memory transaction boundary is asserted in Provider above.
        projects = env.projects(uow)
        projects.semantics.retrieval_settings_factory = lambda: RetrievalSettings('vector')
        with uow.transaction():
            bundle = env.app.state.artifacts.read_ready(env.r.own, snap)
            context = projects.retrieval_context(env.app.state.workspace.path_for(env.r.own), snap, bundle)
            uow.projects.get(env.r.own)  # force SQLAlchemy autobegin
            with pytest.raises(VectorUnavailable):
                context.vector_query('return', 6)
            assert not env.created
