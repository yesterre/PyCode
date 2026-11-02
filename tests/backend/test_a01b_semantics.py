from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from backend.app.config import EmbeddingSettings, AdmissionSettings
from backend.app.core.errors import BackendError
from backend.app.core.models import ProjectStatus
from backend.app.db.models import BackgroundTaskORM
from backend.app.repositories import InMemoryUnitOfWork
from backend.app.runtime import build_project_service
from backend.app.services.admission import AdmissionControl
from backend.app.services.background_tasks import BackgroundTaskService
from backend.app.services.queued_reconciliation import QueuedTaskReconciler
from backend.app.services.snapshot_semantics import SEMANTIC_TASK_TYPE
from backend.app.services.stale_reconciliation import StaleRunningReconciler
from backend.app.tasks import indexing
from pycode.embedding import EmbeddingError
from fake_embedding import FakeEmbeddingProvider
from test_a01_chunks import legacy_bundle
from test_b01_users import uow_factory
from test_b02_auth import auth_context
from test_b04_project_derived_auth import resources
from test_b07_worker_isolation import mutate
from test_postgresql import postgresql_engine


@pytest.fixture
def semantic_env(resources, application, monkeypatch):
    r, app = resources, resources.context.app
    env = SimpleNamespace(r=r, app=app, providers=[], failures={}, transform=None,
                          settings=EmbeddingSettings(enabled=True, api_key='fake-only', batch_size=1))
    app.state.project_operations = application.state.project_operations
    app.state.embedding_settings_factory = lambda: env.settings

    def factory(settings):
        dimensions = {'kinfra-text-embedding-0.6b': 1024, 'kinfra-text-embedding-4b': 2560}
        provider = FakeEmbeddingProvider(model=settings.model, dimension=dimensions[settings.model],
                                         failures=env.failures, transform=env.transform)
        provider.provider = 'tencent_tokenhub'
        env.providers.append(provider)
        return provider

    app.state.embedding_provider_factory = factory

    def projects(uow):
        env.last_uow = uow
        return build_project_service(uow, adapter=app.state.adapter, workspace=app.state.workspace,
            artifacts=app.state.artifacts, operations=app.state.project_operations,
            admission=app.state.admission, dispatcher=app.state.task_dispatcher,
            embedding_settings_factory=app.state.embedding_settings_factory,
            embedding_provider_factory=factory)
    env.projects = projects

    @contextmanager
    def worker_services():
        with r.context.uow_factory() as uow:
            yield BackgroundTaskService(uow), projects(uow)
    monkeypatch.setattr(indexing, 'worker_repository_index_services', worker_services)

    def run(task_id):
        indexing.run_repository_index_task.run(str(task_id))
        with r.context.uow_factory() as uow, uow.transaction():
            return uow.background_tasks.get(UUID(str(task_id)))
    env.run = run
    return env


def post(env, snapshot_id=None, key=None, project=None, headers=None):
    kwargs = {'headers': {**(env.r.headers if headers is None else headers),
                           **({'Idempotency-Key': key} if key else {})}}
    if snapshot_id is not None:
        kwargs['json'] = {'snapshot_id': str(snapshot_id)}
    return env.r.context.client.post(f'/api/v1/projects/{project or env.r.own}/index', **kwargs)


def detail(env, snapshot_id=None):
    response = env.r.context.client.get(
        f'/api/v1/projects/{env.r.own}/snapshots/{snapshot_id or env.r.snapshot.id}', headers=env.r.headers)
    assert response.status_code == 200, response.text
    return response.json()


def automatic(env):
    accepted = post(env)
    assert accepted.status_code == 202, accepted.text
    base = env.run(accepted.json()['task_id'])
    assert base.status == 'completed'
    with env.r.context.uow_factory() as uow, uow.transaction():
        tasks = uow.background_tasks.list_for_snapshot(env.r.own, base.snapshot_id, SEMANTIC_TASK_TYPE)
    return base, tasks


def base_bytes(env, snapshot_id):
    with env.r.context.uow_factory() as uow, uow.transaction():
        snapshot = uow.snapshots.get_for_project(env.r.own, snapshot_id)
    bundle = env.app.state.artifacts.read_ready(env.r.own, snapshot)
    return {path.name: path.read_bytes() for path in bundle.index_path.parent.iterdir()}


def another_snapshot(env):
    """Publish a second valid fixture Snapshot using the real adapter/store contracts."""
    with env.r.context.uow_factory() as uow, uow.transaction():
        snapshot = uow.snapshots.start(env.r.own, 'b' * 40)
    artifacts, adapter = env.app.state.artifacts, env.app.state.adapter
    repo = env.app.state.workspace.path_for(env.r.own)
    bundle = artifacts.build_and_publish(env.r.own, snapshot.id, snapshot.commit_sha,
        lambda index_path, graph_path: adapter.index(repo, index_path=index_path, graph_path=graph_path,
            project_id=env.r.own, snapshot_id=snapshot.id, commit_sha=snapshot.commit_sha), require_chunks=True)
    with env.r.context.uow_factory() as uow, uow.transaction():
        snapshot = uow.snapshots.mark_ready(snapshot.id, index_artifact_path=bundle.index_artifact_path,
            graph_artifact_path=bundle.graph_artifact_path, summary=bundle.summary)
        uow.projects.set_current_snapshot(env.r.own, snapshot.id)
    return snapshot


def test_disabled_and_original_no_body_or_empty_body(semantic_env):
    env = semantic_env
    env.settings = EmbeddingSettings()
    base, tasks = automatic(env)
    assert tasks == [] and env.providers == []
    assert detail(env)['semantic_index']['status'] == 'disabled'
    assert env.r.context.client.post(f'/api/v1/projects/{env.r.own}/index', json={},
                                     headers=env.r.headers).status_code == 202
    assert post(env, base.snapshot_id).status_code == 503


def test_auto_build_and_duplicate_artifact_reuse(semantic_env):
    env = semantic_env
    base, tasks = automatic(env)
    assert len(tasks) == 1 and tasks[0].snapshot_id == base.snapshot_id
    assert detail(env)['semantic_index']['status'] == 'queued'
    before = base_bytes(env, base.snapshot_id)
    assert env.run(tasks[0].id).status == 'completed'
    assert env.run(tasks[0].id).attempt_count == 1
    metadata = detail(env)['semantic_index']
    assert metadata['status'] == 'ready' and metadata['dimension'] == 1024
    assert metadata['artifact']['vector_count'] > 0 and metadata['built_at']
    assert base_bytes(env, base.snapshot_id) == before
    count = len(env.providers)
    repeated = post(env, base.snapshot_id, key='repeat')
    assert repeated.status_code == 202
    env.run(repeated.json()['task_id'])
    assert len(env.providers) == count
    assert 'fake-only' not in json.dumps(metadata) and 'vectors' not in metadata


def test_failed_semantics_preserves_base_and_resumes_batches(semantic_env):
    env = semantic_env
    base, tasks = automatic(env)
    before = base_bytes(env, base.snapshot_id)
    env.failures = {2: EmbeddingError('embedding_timeout')}
    with pytest.raises(BackendError, match='base Snapshot remains available'):
        env.run(tasks[0].id)
    with env.r.context.uow_factory() as uow, uow.transaction():
        assert uow.background_tasks.get(base.id).status == 'completed'
        project = uow.projects.get(env.r.own)
        assert project.status == 'ready' and project.current_snapshot_id == base.snapshot_id
        assert uow.snapshots.get(base.snapshot_id).status == 'ready'
    assert detail(env)['semantic_index']['status'] == 'failed'
    assert base_bytes(env, base.snapshot_id) == before
    env.failures = {}
    retry = post(env, base.snapshot_id)
    env.run(retry.json()['task_id'])
    assert detail(env)['semantic_index']['status'] == 'ready'
    with env.r.context.uow_factory() as uow:
        snapshot = uow.snapshots.get(base.snapshot_id)
    count = len(env.app.state.artifacts.read_chunks(env.r.own, snapshot).chunks)
    assert len(env.providers[-1].calls) == count - 1


def test_historical_backfill_no_base_work_no_current_switch(semantic_env, monkeypatch):
    env = semantic_env
    base, tasks = automatic(env)
    newer = another_snapshot(env)
    before = base_bytes(env, base.snapshot_id)
    new_before = base_bytes(env, newer.id)
    def forbidden(*args, **kwargs): pytest.fail('Historical backfill reached Git/Scanner/Parser/base writes')
    for obj, names in [(env.app.state.workspace, ['synchronize', 'checkout_exact', 'assert_head']),
                       (env.app.state.adapter, ['index']),
                       (env.app.state.artifacts, ['build_and_publish'])]:
        for name in names: monkeypatch.setattr(obj, name, forbidden)
    monkeypatch.setattr('pycode.engine.scan_python_files', forbidden)
    monkeypatch.setattr('pycode.engine.parse_python_file', forbidden)
    accepted = post(env, base.snapshot_id)
    assert accepted.status_code == 202
    completed = env.run(accepted.json()['task_id'])
    assert completed.snapshot_id == base.snapshot_id
    assert detail(env, base.snapshot_id)['semantic_index']['status'] == 'ready'
    assert detail(env, newer.id)['semantic_index']['status'] == 'not_built'
    with env.r.context.uow_factory() as uow, uow.transaction():
        assert uow.projects.get(env.r.own).current_snapshot_id == newer.id
    assert base_bytes(env, base.snapshot_id) == before
    assert base_bytes(env, newer.id) == new_before
    # The earlier automatically reserved task also targets history safely.
    assert env.run(tasks[0].id).snapshot_id == base.snapshot_id


def test_models_and_snapshots_independent(semantic_env):
    env = semantic_env
    base, tasks = automatic(env)
    env.run(tasks[0].id)
    first = detail(env)['semantic_index']['fingerprint']
    env.settings = replace(env.settings, model='kinfra-text-embedding-4b')
    accepted = post(env, base.snapshot_id)
    env.run(accepted.json()['task_id'])
    metadata = detail(env)['semantic_index']
    assert metadata['dimension'] == 2560 and metadata['fingerprint'] != first
    assert {v['dimension'] for v in metadata['versions']} == {1024, 2560}
    newer = another_snapshot(env)
    accepted = post(env, newer.id)
    env.run(accepted.json()['task_id'])
    assert detail(env, newer.id)['semantic_index']['fingerprint'] != metadata['fingerprint']


def test_missing_chunks_legacy_and_auth_boundaries(semantic_env, monkeypatch):
    env = semantic_env
    base, _ = automatic(env)
    with env.r.context.uow_factory() as uow, uow.transaction():
        snapshot = uow.snapshots.get(base.snapshot_id)
    legacy_bundle(env.app.state.artifacts, snapshot)
    rejected = post(env, snapshot.id)
    assert rejected.status_code == 409
    assert detail(env)['status'] == 'ready'
    with monkeypatch.context() as patch:
        patch.setattr(env.app.state.artifacts, 'read_chunks', lambda *a: pytest.fail('unauthorized artifact read'))
        assert post(env, snapshot.id, headers=env.r.other_headers).status_code == 404
        assert post(env, env.r.foreign_snapshot.id).status_code == 404
        assert post(env, uuid4()).status_code == 404
    env.r.context.client.post(f'/api/v1/projects/{env.r.own}/archive', headers=env.r.headers)
    assert post(env, snapshot.id).status_code == 409
    assert env.providers == []


def test_idempotency_quota_and_config_change(semantic_env):
    env = semantic_env
    base, _ = automatic(env)
    first = post(env, base.snapshot_id, key='fixed')
    assert post(env, base.snapshot_id, key='fixed').json() == first.json()
    newer = another_snapshot(env)
    assert post(env, newer.id, key='fixed').status_code == 409
    env.settings = replace(env.settings, model='kinfra-text-embedding-4b')
    with pytest.raises(BackendError, match='target changed'):
        env.run(first.json()['task_id'])
    assert env.providers == []
    class RejectLimiter:
        def check(self, *args): raise BackendError('rate_limited', 'Request rate limit exceeded.')
        def close(self): pass
    env.app.state.admission = AdmissionControl(AdmissionSettings(active_tasks=20), RejectLimiter())
    assert post(env, newer.id).status_code == 429


def test_stale_recovery_and_fenced_writer(semantic_env):
    env = semantic_env
    _, tasks = automatic(env)
    task = tasks[0]
    with env.r.context.uow_factory() as uow, uow.transaction():
        claimed = uow.background_tasks.claim(task.id).task
        mutate(uow, 'background_tasks', BackgroundTaskORM, task.id,
               lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    with env.r.context.uow_factory() as uow:
        projects = env.projects(uow)
        result = StaleRunningReconciler(uow, env.app.state.project_operations,
            semantic_ready=projects.semantics.task_ready).reconcile()
    assert result.requeued == 1 and result.completed == 0
    assert env.run(task.id).attempt_count == 2
    with env.r.context.uow_factory() as uow, uow.transaction():
        assert uow.background_tasks.mark_failed(task.id, execution_token=claimed.execution_token,
            error_code='bad', error_message='stale') is None


def test_queued_reconcile_and_auto_publish_failure_keep_base(semantic_env, monkeypatch):
    env = semantic_env
    dispatcher = env.app.state.task_dispatcher
    enqueue = dispatcher.enqueue_repository_index
    accepted = post(env)
    def unavailable(task_id): raise RuntimeError('injected broker outage')
    monkeypatch.setattr(dispatcher, 'enqueue_repository_index', unavailable)
    base = env.run(accepted.json()['task_id'])
    assert base.status == 'completed'
    with env.r.context.uow_factory() as uow, uow.transaction():
        task = uow.background_tasks.list_for_snapshot(env.r.own, base.snapshot_id, SEMANTIC_TASK_TYPE)[0]
    monkeypatch.setattr(dispatcher, 'enqueue_repository_index', enqueue)
    future = datetime.now(timezone.utc) + timedelta(minutes=5)
    with env.r.context.uow_factory() as uow:
        result = QueuedTaskReconciler(uow, dispatcher, clock=lambda: future).run()
    assert result.confirmed_count >= 1
    env.run(task.id)
    assert detail(env)['semantic_index']['status'] == 'ready'


def test_building_projection_and_no_db_transaction_during_provider(semantic_env):
    env = semantic_env
    _, tasks = automatic(env)
    observations = []
    def inspect(result):
        session = getattr(env.last_uow, 'session', None)
        assert session is None or not session.in_transaction()
        observations.append(detail(env)['semantic_index']['status'])
        return result
    env.transform = inspect
    env.run(tasks[0].id)
    assert observations and set(observations) == {'building'}


def test_configuration_failure_keeps_base_ready(semantic_env):
    env = semantic_env
    def invalid(): raise ValueError('invalid fake configuration')
    env.app.state.embedding_settings_factory = invalid
    # The worker factory follows the same application settings factory.
    base, tasks = automatic(env)
    assert len(tasks) == 1
    with pytest.raises(BackendError, match='configuration is invalid'):
        env.run(tasks[0].id)
    assert detail(env)['status'] == 'ready'
    assert detail(env)['semantic_index']['status'] == 'failed'
    assert env.providers == []


def test_new_commit_auto_task_cannot_reuse_previous_snapshot(semantic_env, monkeypatch):
    from backend.app.infrastructure.repositories import RepositoryRevision
    env = semantic_env
    first, tasks = automatic(env)
    env.run(tasks[0].id)
    previous = detail(env)['semantic_index']['fingerprint']
    source = env.app.state.workspace.source
    monkeypatch.setattr(source, 'update', lambda *a, **k: RepositoryRevision('default', 'b' * 40))
    monkeypatch.setattr(source, 'resolve_head', lambda *a, **k: 'b' * 40)
    monkeypatch.setattr(source, 'checkout_exact', lambda *a, **k: None)
    monkeypatch.setattr(source, 'protect_snapshot', lambda *a, **k: None)
    second, tasks = automatic(env)
    assert second.snapshot_id != first.snapshot_id
    count = len(env.providers)
    env.run(tasks[0].id)
    assert len(env.providers) == count + 1
    assert detail(env, second.snapshot_id)['semantic_index']['fingerprint'] != previous
    assert detail(env, first.snapshot_id)['semantic_index']['fingerprint'] == previous


def test_lost_lease_fences_checkpoint_and_publish(semantic_env):
    env = semantic_env
    base, tasks = automatic(env)
    task = tasks[0]
    changed = []
    def lose(result):
        if not changed:
            changed.append(True)
            with env.r.context.uow_factory() as uow, uow.transaction():
                mutate(uow, 'background_tasks', BackgroundTaskORM, task.id, execution_token=uuid4())
        return result
    env.transform = lose
    assert env.run(task.id).status == 'running'
    assert detail(env)['semantic_index']['status'] == 'building'
    assert detail(env)['status'] == 'ready'
    with env.r.context.uow_factory() as uow, uow.transaction():
        mutate(uow, 'background_tasks', BackgroundTaskORM, task.id,
               lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    with env.r.context.uow_factory() as uow:
        assert StaleRunningReconciler(uow, env.app.state.project_operations).reconcile().requeued == 1
    env.transform = None
    assert env.run(task.id).status == 'completed'


def test_stale_after_publication_completes_without_provider(semantic_env):
    env = semantic_env
    _, tasks = automatic(env)
    task = tasks[0]
    from backend.app.infrastructure.execution import InlineExecutionOwnership
    with env.r.context.uow_factory() as uow, uow.transaction():
        task = uow.background_tasks.claim(task.id).task
    with env.r.context.uow_factory() as uow:
        projects = env.projects(uow)
        ownership = InlineExecutionOwnership(task, lambda *a: True)
        projects.semantics.build_task(task, ownership, projects.operations)
    with env.r.context.uow_factory() as uow, uow.transaction():
        mutate(uow, 'background_tasks', BackgroundTaskORM, task.id,
               lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    count = len(env.providers)
    with env.r.context.uow_factory() as uow:
        result = StaleRunningReconciler(uow, env.app.state.project_operations,
            semantic_ready=env.projects(uow).semantics.task_ready).reconcile()
    assert result.completed == 1 and result.requeued == 0
    assert env.run(task.id).attempt_count == 1 and len(env.providers) == count


def test_corruption_and_cross_project_do_not_reach_provider(semantic_env):
    env = semantic_env
    _, tasks = automatic(env)
    env.run(tasks[0].id)
    with env.r.context.uow_factory() as uow, uow.transaction():
        snapshot = uow.snapshots.get(env.r.snapshot.id)
    bundle = env.app.state.artifacts.read_ready(env.r.own, snapshot)
    bundle.chunks_path.write_text('{}', encoding='utf-8')
    count = len(env.providers)
    assert post(env, snapshot.id).status_code == 409
    assert len(env.providers) == count
    assert detail(env)['semantic_index']['status'] == 'unavailable'
    assert detail(env)['status'] == 'ready'


def test_idempotency_spans_index_modes_and_replay_needs_no_artifacts(semantic_env, monkeypatch):
    env = semantic_env
    accepted = post(env, key='base-key')
    base = env.run(accepted.json()['task_id'])
    assert post(env, base.snapshot_id, key='base-key').status_code == 409
    semantic = post(env, base.snapshot_id, key='semantic-key')
    assert post(env, key='semantic-key').status_code == 409
    with monkeypatch.context() as patch:
        patch.setattr(env.app.state.artifacts, 'read_chunks', lambda *a: pytest.fail('idempotent replay read artifact'))
        env.settings = replace(env.settings, model='kinfra-text-embedding-4b')
        assert post(env, base.snapshot_id, key='semantic-key').json() == semantic.json()


def test_auto_semantic_quota_failure_keeps_base_success(semantic_env):
    env = semantic_env
    class AllowLimiter:
        def check(self, *args): pass
        def close(self): pass
    env.app.state.admission = AdmissionControl(AdmissionSettings(active_tasks=1), AllowLimiter())
    base, tasks = automatic(env)
    assert base.status == 'completed' and tasks[0].status == 'failed'
    assert tasks[0].error_code == 'task_quota_exceeded'
    assert detail(env)['status'] == 'ready'
    # After the base task releases its quota slot, manual retry can be admitted.
    retry = post(env, base.snapshot_id)
    assert retry.status_code == 202
    env.run(retry.json()['task_id'])
    assert detail(env)['semantic_index']['status'] == 'ready'


def test_two_users_concurrent_semantic_workers_keep_project_bindings(semantic_env):
    from concurrent.futures import ThreadPoolExecutor
    env = semantic_env
    first, tasks = automatic(env)
    accepted = post(env, project=env.r.foreign, headers=env.r.other_headers)
    assert accepted.status_code == 202
    second = env.run(accepted.json()['task_id'])
    with env.r.context.uow_factory() as uow, uow.transaction():
        other = uow.background_tasks.list_for_snapshot(env.r.foreign, second.snapshot_id, SEMANTIC_TASK_TYPE)[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        completed = list(pool.map(env.run, [tasks[0].id, other.id]))
    assert all(task.status == 'completed' for task in completed)
    assert {task.snapshot_id for task in completed} == {first.snapshot_id, second.snapshot_id}
    assert len(env.providers) == 2
    with env.r.context.uow_factory() as uow, uow.transaction():
        foreign_snapshot = uow.snapshots.get_for_project(env.r.foreign, second.snapshot_id)
    with env.r.context.uow_factory() as uow:
        foreign = env.projects(uow).semantics.describe(foreign_snapshot)
    own = detail(env)['semantic_index']
    assert foreign['status'] == own['status'] == 'ready'
    assert foreign['fingerprint'] != own['fingerprint']
    response = env.r.context.client.get(f'/api/v1/projects/{env.r.foreign}/snapshots/{second.snapshot_id}',
                                        headers=env.r.headers)
    assert response.status_code == 404


@pytest.mark.parametrize('enabled', [False, True])
def test_unready_snapshot_projection_does_not_access_artifacts(semantic_env, monkeypatch, enabled):
    env = semantic_env
    env.settings = replace(env.settings, enabled=enabled)
    def forbidden(*args, **kwargs): pytest.fail('Unready metadata read accessed Artifact')
    monkeypatch.setattr(env.app.state.artifacts, 'read_chunks', forbidden)
    result = detail(env)
    assert result['status'] == 'indexing'
    assert result['semantic_index']['status'] == ('not_built' if enabled else 'disabled')
