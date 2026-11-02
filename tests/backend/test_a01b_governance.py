"""Automatic indexing obeys the existing B08/B11 task lifecycle."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from celery.exceptions import Reject

from backend.app.config import AdmissionSettings
from backend.app.core.errors import BackendError
from backend.app.db.models import BackgroundTaskORM
from backend.app.infrastructure.redis import RedisRateLimiter
from backend.app.services.admission import AdmissionControl
from backend.app.services.snapshot_semantics import SEMANTIC_TASK_TYPE, SnapshotSemantics
from backend.app.services.stale_reconciliation import StaleRunningReconciler
from test_a01b_semantics import (
    semantic_env, automatic, post, detail, base_bytes,
    resources, auth_context, uow_factory, postgresql_engine,
)
from test_b07_worker_isolation import mutate


def archive(env):
    response = env.r.context.client.post(f'/api/v1/projects/{env.r.own}/archive', headers=env.r.headers)
    assert response.status_code == 200, response.text


def tasks_for(env, snapshot_id):
    with env.r.context.uow_factory() as uow, uow.transaction():
        return uow.background_tasks.list_for_snapshot(env.r.own, snapshot_id, SEMANTIC_TASK_TYPE)


def recover(env):
    with env.r.context.uow_factory() as uow:
        semantics = env.projects(uow).semantics
        return StaleRunningReconciler(uow, env.app.state.project_operations,
            semantic_ready=semantics.task_ready, index_completion=semantics.completion_for).reconcile()


def expire(env, task_id):
    with env.r.context.uow_factory() as uow, uow.transaction():
        mutate(uow, 'background_tasks', BackgroundTaskORM, task_id,
               lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5))


def limited(env, active_tasks=1):
    calls = []
    class Limiter:
        def check(self, *args): calls.append(args)
        def close(self): pass
    policy = AdmissionControl(AdmissionSettings(active_tasks=active_tasks), Limiter())
    env.app.state.admission = policy
    return policy, calls


def test_production_worker_factories_wire_actual_admission_control(monkeypatch):
    # Only replace the DB transport. Do not inject an AdmissionControl or replace
    # build_project_service: exercise the real production composition functions.
    from backend.app.tasks import runtime
    sessions = []
    class Session:
        def __init__(self): self.closed = False; sessions.append(self)
        def in_transaction(self): return False
        def close(self): self.closed = True
    monkeypatch.setattr(runtime._database, 'open_session', Session)
    assert isinstance(runtime._admission, AdmissionControl)
    assert isinstance(runtime._admission.limiter, RedisRateLimiter)
    with runtime.worker_repository_index_services() as (tasks, projects):
        assert projects.admission is runtime._admission
        assert projects.semantics.admission is runtime._admission
        assert projects.uow is tasks.unit_of_work
    with runtime.worker_stale_running_reconciler() as stale:
        assert stale.index_completion.__self__.admission is runtime._admission
        assert stale.index_completion.__self__.dispatcher is runtime._dispatcher
    assert len(sessions) == 2 and all(session.closed for session in sessions)


def test_accepted_before_archive_indexes_and_embeds_with_one_admission(semantic_env):
    env = semantic_env
    _, calls = limited(env)
    accepted = post(env, key='accepted-before-archive')
    assert accepted.status_code == 202
    archive(env)
    parent = env.run(accepted.json()['task_id'])
    children = tasks_for(env, parent.snapshot_id)
    assert parent.status == 'completed' and len(children) == 1 and children[0].status == 'queued'
    before = base_bytes(env, parent.snapshot_id)
    assert env.run(children[0].id).status == 'completed'
    assert env.run(parent.id).attempt_count == env.run(children[0].id).attempt_count == 1
    assert len(calls) == 1 and base_bytes(env, parent.snapshot_id) == before
    assert detail(env)['semantic_index']['status'] == 'ready'
    rejected = post(env, key='new-after-archive')
    assert rejected.status_code == 409 and rejected.json()['detail']['code'] == 'project_archived'
    assert post(env, key='accepted-before-archive').json()['task_id'] == str(parent.id)
    with env.r.context.uow_factory() as uow, uow.transaction():
        assert uow.projects.get(env.r.own).archived_at is not None
        assert uow.background_tasks.lock_owner_and_count_active(env.r.owner) == 0


def test_archive_during_embedding_does_not_cancel_later_batches(semantic_env):
    env = semantic_env
    _, children = automatic(env)
    archived = []
    def transform(result):
        if not archived:
            archive(env)
            archived.append(True)
            assert post(env).status_code == 409
        return result
    env.transform = transform
    assert env.run(children[0].id).status == 'completed'
    assert len(env.providers[0].calls) > 1
    assert detail(env)['semantic_index']['status'] == 'ready' and detail(env)['status'] == 'ready'


def test_archived_interrupted_semantic_task_resumes_checkpoint(semantic_env):
    env = semantic_env
    _, calls = limited(env)
    _, children = automatic(env)
    class WorkerStopped(BaseException): pass
    env.failures = {2: WorkerStopped()}
    with pytest.raises(WorkerStopped): env.run(children[0].id)
    with env.r.context.uow_factory() as uow, uow.transaction():
        old = uow.background_tasks.get(children[0].id)
        assert old.status == 'running'
    archive(env)
    expire(env, old.id)
    result = recover(env)
    assert result.requeued == 1 and result.failed == 0
    env.failures = {}
    completed = env.run(old.id)
    assert completed.status == 'completed' and completed.attempt_count == 2
    assert completed.execution_token != old.execution_token
    metadata = detail(env)['semantic_index']
    assert metadata['status'] == 'ready'
    assert len(env.providers[-1].calls) == metadata['artifact']['vector_count'] - 1
    assert len(calls) == 1
    assert post(env).status_code == 409


def test_parent_completion_and_child_reservation_rollback_then_recover_after_archive(semantic_env, monkeypatch):
    env = semantic_env
    _, calls = limited(env)
    accepted = post(env)
    parent_id = UUID(accepted.json()['task_id'])
    original = SnapshotSemantics.reserve_automatic
    interrupted = []
    def reserve_then_crash(service, snapshot, fingerprint, source_task_id):
        task = original(service, snapshot, fingerprint, source_task_id)
        if source_task_id == parent_id and not interrupted:
            interrupted.append(True)
            raise BackendError('database_unavailable', 'Injected terminal-write interruption.')
        return task
    with monkeypatch.context() as patch:
        patch.setattr(SnapshotSemantics, 'reserve_automatic', reserve_then_crash)
        with pytest.raises(Reject) as failure: env.run(parent_id)
        assert failure.value.requeue is True
    with env.r.context.uow_factory() as uow, uow.transaction():
        parent = uow.background_tasks.get(parent_id)
        assert parent.status == 'running'
        assert uow.snapshots.get(parent.snapshot_id).status == 'ready'
    assert tasks_for(env, parent.snapshot_id) == []
    archive(env)
    expire(env, parent_id)
    monkeypatch.setattr('pycode.engine.scan_python_files', lambda *a: pytest.fail('recovery rescanned source'))
    monkeypatch.setattr('pycode.engine.parse_python_file', lambda *a, **k: pytest.fail('recovery reparsed source'))
    result = recover(env)
    assert result.completed == 1 and result.failed == 0
    children = tasks_for(env, parent.snapshot_id)
    assert len(children) == 1 and children[0].status == 'queued'
    assert env.run(children[0].id).status == 'completed' and len(calls) == 1
    assert detail(env)['semantic_index']['status'] == 'ready'


@pytest.mark.parametrize('association', ['project', 'snapshot'])
def test_archived_recovery_rejects_foreign_binding_before_artifact_access(semantic_env, monkeypatch, association):
    env = semantic_env
    _, children = automatic(env)
    archive(env)
    with env.r.context.uow_factory() as uow, uow.transaction():
        task = uow.background_tasks.claim(children[0].id).task
        values = {'snapshot_id': env.r.foreign_snapshot.id} if association == 'snapshot' else {'project_id': env.r.foreign}
        mutate(uow, 'background_tasks', BackgroundTaskORM, task.id, **values)
        victim = uow.snapshots.get(env.r.foreign_snapshot.id)
    expire(env, task.id)
    monkeypatch.setattr(env.app.state.artifacts, 'read_ready', lambda *a: pytest.fail('foreign Artifact read'))
    result = recover(env)
    assert result.failed == 1 and result.requeued == result.completed == 0
    assert env.providers == []
    with env.r.context.uow_factory() as uow, uow.transaction():
        assert uow.snapshots.get(victim.id) == victim


def test_continuation_still_enforces_active_limit_when_policy_changes(semantic_env):
    env = semantic_env
    policy, calls = limited(env, active_tasks=4)
    accepted = post(env)
    assert accepted.status_code == 202
    with env.r.context.uow_factory() as uow, uow.transaction():
        uow.background_tasks.create('health', project_id=env.r.own)
    policy.settings = AdmissionSettings(active_tasks=1)
    parent = env.run(accepted.json()['task_id'])
    child = tasks_for(env, parent.snapshot_id)[0]
    assert parent.status == 'completed' and child.status == 'failed'
    assert child.error_code == 'task_quota_exceeded' and env.providers == []
    assert len(calls) == 1 and detail(env)['status'] == 'ready'


def test_same_commit_normal_index_fills_missing_semantics_without_rebuilding_base(semantic_env, monkeypatch):
    from backend.app.config import EmbeddingSettings
    env = semantic_env
    env.settings = EmbeddingSettings()
    first, children = automatic(env)
    assert children == []
    before = base_bytes(env, first.snapshot_id)
    env.settings = EmbeddingSettings(enabled=True, api_key='fake-only', batch_size=1)
    monkeypatch.setattr(env.app.state.adapter, 'index', lambda *a, **k: pytest.fail('rebuilt reusable base'))
    monkeypatch.setattr('pycode.engine.parse_python_file', lambda *a, **k: pytest.fail('reparsed reusable base'))
    same, children = automatic(env)
    assert same.snapshot_id == first.snapshot_id and len(children) == 1
    assert env.run(children[0].id).status == 'completed'
    assert base_bytes(env, first.snapshot_id) == before
    assert detail(env)['semantic_index']['status'] == 'ready'
