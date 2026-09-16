from dataclasses import replace
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from redis import Redis
from redis.exceptions import RedisError

from backend.app.config import QueuedReconciliationSettings, StaleReconciliationSettings
from backend.app.core.errors import BackendError
from backend.app.core.models import (
    AgentRunStatus, BackgroundTaskStatus, IndexSummary, ProjectStatus,
)
from backend.app.infrastructure.projects import (
    InMemoryProjectLockBackend, ProjectOperationLocks, RedisProjectLockBackend,
)
from backend.app.infrastructure.execution import ExecutionOwnership
from backend.app.infrastructure.celery import create_celery_app
from backend.app.repositories import InMemoryPersistence, InMemoryUnitOfWork
from backend.app.runtime import build_project_service
from backend.app.services.background_tasks import (
    AGENT_RUN_TASK_TYPE, REPOSITORY_INDEX_TASK_TYPE, BackgroundTaskService,
)
from backend.app.services.agent_runs import AgentRunService
from backend.app.services.stale_reconciliation import (
    RECONCILE_STALE_RUNNING_CELERY_TASK_NAME, StaleRunningReconciler,
)
from backend.app.services.queued_reconciliation import QueuedTaskReconciler


EXPIRED = datetime.now(timezone.utc) - timedelta(minutes=10)
SETTINGS = StaleReconciliationSettings(
    interval_seconds=30, batch_size=100, max_execution_attempts=2,
)


def _locks():
    return ProjectOperationLocks(
        InMemoryProjectLockBackend(), ttl_seconds=300,
        renewal_interval_seconds=60,
    )


def _ready_project(state: InMemoryPersistence):
    uow = InMemoryUnitOfWork(state)
    with uow.transaction():
        project = uow.projects.create("demo", "https://github.com/example/demo.git")
        snapshot = uow.snapshots.start(project.id, "a" * 40)
        snapshot = uow.snapshots.mark_ready(
            snapshot.id, index_artifact_path="index.json",
            graph_artifact_path="graph.json", summary=IndexSummary(1, 1, 0),
        )
        uow.projects.set_current_snapshot(project.id, snapshot.id)
        project = uow.projects.set_status(project.id, ProjectStatus.READY)
    return project, snapshot


def _stale(state: InMemoryPersistence, task_id) -> None:
    state.background_tasks[task_id] = replace(
        state.background_tasks[task_id], lease_expires_at=EXPIRED,
    )


def test_owner_claim_heartbeat_and_terminal_fencing():
    state = InMemoryPersistence()
    uow = InMemoryUnitOfWork(state)
    with uow.transaction():
        task = uow.background_tasks.create("health")
        claim = uow.background_tasks.claim(
            task.id, lease_ttl_seconds=420, worker_id="worker:1:req",
        )
    owned = claim.task
    assert owned is not None
    assert owned.execution_token is not None
    assert owned.lease_expires_at is not None
    assert owned.last_heartbeat_at is not None
    assert owned.worker_id == "worker:1:req"
    assert owned.attempt_count == 1

    with uow.transaction():
        queued = uow.background_tasks.create("health")
        assert uow.background_tasks.heartbeat(
            queued.id, uuid4(), lease_ttl_seconds=420,
        ) is None
        assert uow.background_tasks.heartbeat(
            task.id, uuid4(), lease_ttl_seconds=420,
        ) is None
        renewed = uow.background_tasks.heartbeat(
            task.id, owned.execution_token, lease_ttl_seconds=420,
        )
        assert renewed is not None
        assert uow.background_tasks.mark_completed(
            task.id, execution_token=uuid4(),
        ) is None
        assert uow.background_tasks.mark_failed(
            task.id, error_code="wrong", error_message="wrong",
            execution_token=uuid4(),
        ) is None
        completed = uow.background_tasks.mark_completed(
            task.id, execution_token=owned.execution_token,
        )
    assert completed is not None
    assert completed.status == BackgroundTaskStatus.COMPLETED
    assert completed.lease_expires_at is None
    with uow.transaction():
        assert uow.background_tasks.heartbeat(
            task.id, owned.execution_token, lease_ttl_seconds=420,
        ) is None


def test_confirmed_queued_wrong_delivery_is_terminalized_not_rolled_back():
    state = InMemoryPersistence()
    uow = InMemoryUnitOfWork(state)
    with uow.transaction():
        task = uow.background_tasks.create("repository_index")
    with pytest.raises(BackendError) as failure:
        BackgroundTaskService(uow).execute_health(task.id)
    assert failure.value.code == "background_task_state_conflict"
    persisted = state.background_tasks[task.id]
    assert persisted.status == BackgroundTaskStatus.FAILED
    assert persisted.attempt_count == 1


def test_invalid_agent_delivery_terminalizes_confirmed_queued_task():
    state = InMemoryPersistence()
    project, _ = _ready_project(state)
    uow = InMemoryUnitOfWork(state)
    with uow.transaction():
        task = uow.background_tasks.create(
            AGENT_RUN_TASK_TYPE, project_id=project.id,
        )
    service = AgentRunService(
        uow, BackgroundTaskService(uow), projects=object(), adapter=object(),
        agent_workspace=object(),
    )
    with pytest.raises(BackendError) as failure:
        service.execute(task.id)
    assert failure.value.code == "background_task_consistency_error"
    persisted = state.background_tasks[task.id]
    assert persisted.status == BackgroundTaskStatus.FAILED
    assert persisted.attempt_count == 1


def test_database_outage_leaves_claimed_lease_for_scanner_and_reject_path():
    state = InMemoryPersistence()
    uow = InMemoryUnitOfWork(state)
    with uow.transaction():
        task = uow.background_tasks.create("health")

    def unavailable():
        raise BackendError("database_unavailable", "Database unavailable.")

    with pytest.raises(BackendError) as failure:
        BackgroundTaskService(uow).execute_health(task.id, unavailable)
    assert failure.value.code == "database_unavailable"
    persisted = state.background_tasks[task.id]
    assert persisted.status == BackgroundTaskStatus.RUNNING
    assert persisted.execution_token is not None
    assert persisted.lease_expires_at is not None


def test_heartbeat_coordinator_distinguishes_database_failure_and_lost_owner(
    monkeypatch,
):
    state = InMemoryPersistence()
    uow = InMemoryUnitOfWork(state)
    with uow.transaction():
        claimed = uow.background_tasks.claim(
            uow.background_tasks.create("health").id,
        ).task
    assert claimed is not None

    class Session:
        def in_transaction(self):
            return False

        def close(self):
            pass

    class Database:
        def __init__(self):
            self.calls = 0

        def open_session(self):
            self.calls += 1
            return Session()

    class FakeRepository:
        result = claimed

        def heartbeat(self, *args, **kwargs):
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    repository = FakeRepository()

    class FakeUow:
        background_tasks = repository

        @contextmanager
        def transaction(self):
            yield

    database = Database()
    monkeypatch.setattr(
        "backend.app.infrastructure.execution.SqlAlchemyUnitOfWork",
        lambda session: FakeUow(),
    )
    ownership = ExecutionOwnership(
        database, claimed,
        settings=type("Settings", (), {
            "ttl_seconds": 420, "heartbeat_interval_seconds": 60,
        })(),
    )
    ownership.ensure_owned()
    assert database.calls == 1
    repository.result = RuntimeError("temporary database outage")
    with pytest.raises(BackendError) as unavailable:
        ownership.ensure_owned()
    assert unavailable.value.code == "database_unavailable"
    repository.result = None
    with pytest.raises(BackendError) as lost:
        ownership.ensure_owned()
    assert lost.value.code == "worker_execution_lost"
    with ownership:
        assert ownership._thread is not None and ownership._thread.is_alive()
    assert ownership._thread is not None and not ownership._thread.is_alive()


def test_repository_stale_without_target_is_failed():
    state = InMemoryPersistence()
    project, _ = _ready_project(state)
    uow = InMemoryUnitOfWork(state)
    with uow.transaction():
        task = uow.background_tasks.create(
            REPOSITORY_INDEX_TASK_TYPE, project_id=project.id,
        )
        task = uow.background_tasks.claim(task.id).task
    assert task is not None
    _stale(state, task.id)

    result = StaleRunningReconciler(uow, _locks(), SETTINGS).reconcile()

    assert result.failed == 1
    assert state.background_tasks[task.id].error_code == "worker_execution_lost"


def test_legacy_running_row_without_token_or_lease_is_conservatively_failed():
    state = InMemoryPersistence()
    uow = InMemoryUnitOfWork(state)
    with uow.transaction():
        task = uow.background_tasks.create("health")
        task = uow.background_tasks.claim(task.id).task
    assert task is not None
    state.background_tasks[task.id] = replace(
        task, execution_token=None, lease_expires_at=None,
        last_heartbeat_at=None, worker_id=None,
    )
    result = StaleRunningReconciler(uow, _locks(), SETTINGS).reconcile()
    assert result.failed == 1
    assert state.background_tasks[task.id].error_code == "worker_execution_lost"


def test_repository_bound_target_requeues_then_new_claim_is_attempt_two():
    state = InMemoryPersistence()
    project, snapshot = _ready_project(state)
    uow = InMemoryUnitOfWork(state)
    with uow.transaction():
        task = uow.background_tasks.create(
            REPOSITORY_INDEX_TASK_TYPE, project_id=project.id,
        )
        task = uow.background_tasks.claim(task.id).task
        assert task is not None and task.execution_token is not None
        task = uow.background_tasks.bind_snapshot_if_owner(
            task.id, task.execution_token, snapshot.id,
        )
    assert task is not None
    # A ready Snapshot that is no longer activated is an unsafe mismatch.
    state.snapshots[snapshot.id] = replace(snapshot, status="indexing")
    state.projects[project.id] = replace(project, current_snapshot_id=None)
    _stale(state, task.id)

    result = StaleRunningReconciler(uow, _locks(), SETTINGS).reconcile()
    queued = state.background_tasks[task.id]
    assert result.requeued == 1
    assert queued.status == BackgroundTaskStatus.QUEUED
    assert queued.snapshot_id == snapshot.id
    assert queued.execution_token is None
    assert queued.enqueue_confirmed_at is None
    with uow.transaction():
        second = uow.background_tasks.claim(queued.id).task
    assert second is not None
    assert second.attempt_count == 2
    assert second.execution_token != task.execution_token


def test_stale_requeue_is_published_only_by_queued_reconciler():
    state = InMemoryPersistence()
    project, snapshot = _ready_project(state)
    uow = InMemoryUnitOfWork(state)
    with uow.transaction():
        task = uow.background_tasks.create(
            REPOSITORY_INDEX_TASK_TYPE, project_id=project.id,
            snapshot_id=snapshot.id,
        )
        task = uow.background_tasks.claim(task.id).task
    assert task is not None
    state.snapshots[snapshot.id] = replace(snapshot, status="indexing")
    state.projects[project.id] = replace(project, current_snapshot_id=None)
    _stale(state, task.id)
    StaleRunningReconciler(uow, _locks(), SETTINGS).reconcile()

    class Dispatcher:
        calls = []

        def enqueue_health(self, task_id):
            raise AssertionError("unexpected task type")

        def enqueue_repository_index(self, task_id):
            self.calls.append(task_id)

        def enqueue_agent_run(self, task_id):
            raise AssertionError("unexpected task type")

    dispatcher = Dispatcher()
    later = datetime.now(timezone.utc) + timedelta(minutes=1)
    result = QueuedTaskReconciler(
        uow, dispatcher, QueuedReconciliationSettings(0, 30, 100, 120),
        clock=lambda: later,
    ).run()
    republished = state.background_tasks[task.id]
    assert result.confirmed_count == 1
    assert dispatcher.calls == [task.id]
    assert republished.status == BackgroundTaskStatus.QUEUED
    assert republished.enqueue_attempt_count == 1
    assert republished.enqueue_confirmed_at == later


def test_repository_post_activation_only_completes_task():
    state = InMemoryPersistence()
    project, snapshot = _ready_project(state)
    uow = InMemoryUnitOfWork(state)
    with uow.transaction():
        task = uow.background_tasks.create(
            REPOSITORY_INDEX_TASK_TYPE, project_id=project.id,
            snapshot_id=snapshot.id,
        )
        task = uow.background_tasks.claim(task.id).task
    assert task is not None
    _stale(state, task.id)
    result = StaleRunningReconciler(uow, _locks(), SETTINGS).reconcile()
    assert result.completed == 1
    assert state.background_tasks[task.id].status == BackgroundTaskStatus.COMPLETED


@pytest.mark.parametrize("unsafe", ["mismatch", "failed", "attempt_exhausted"])
def test_repository_unsafe_or_exhausted_recovery_is_failed(unsafe):
    state = InMemoryPersistence()
    project, snapshot = _ready_project(state)
    uow = InMemoryUnitOfWork(state)
    with uow.transaction():
        task = uow.background_tasks.create(
            REPOSITORY_INDEX_TASK_TYPE, project_id=project.id,
            snapshot_id=snapshot.id,
        )
        task = uow.background_tasks.claim(task.id).task
    assert task is not None
    state.snapshots[snapshot.id] = replace(
        snapshot, status="failed" if unsafe == "failed" else "indexing",
    )
    if unsafe == "mismatch":
        other = uuid4()
        state.projects[project.id] = replace(project, current_snapshot_id=other)
    elif unsafe == "attempt_exhausted":
        state.background_tasks[task.id] = replace(task, attempt_count=2)
    _stale(state, task.id)
    result = StaleRunningReconciler(uow, _locks(), SETTINGS).reconcile()
    assert result.failed == 1
    assert state.background_tasks[task.id].status == BackgroundTaskStatus.FAILED


def _running_agent(state: InMemoryPersistence):
    project, snapshot = _ready_project(state)
    uow = InMemoryUnitOfWork(state)
    with uow.transaction():
        run = uow.agent_runs.create_queued(
            project.id, snapshot.id, task="analyze", run_type="agent", model=None,
        )
        task = uow.background_tasks.create(
            AGENT_RUN_TASK_TYPE, project_id=project.id,
            snapshot_id=snapshot.id, agent_run_id=run.id,
        )
        task = uow.background_tasks.claim(task.id).task
        run = uow.agent_runs.mark_running(run.id)
    assert task is not None
    _stale(state, task.id)
    return uow, project, snapshot, run, task


def test_agent_pre_core_stale_requeues_both_rows_atomically():
    state = InMemoryPersistence()
    uow, _, _, run, task = _running_agent(state)
    result = StaleRunningReconciler(uow, _locks(), SETTINGS).reconcile()
    assert result.requeued == 1
    assert state.background_tasks[task.id].status == BackgroundTaskStatus.QUEUED
    assert state.agent_runs[run.id].status == AgentRunStatus.QUEUED
    assert state.agent_runs[run.id].core_started_at is None


def test_agent_post_core_stale_fails_without_requeue():
    state = InMemoryPersistence()
    uow, _, _, run, task = _running_agent(state)
    with uow.transaction():
        marked = uow.agent_runs.mark_core_started(run.id)
    result = StaleRunningReconciler(uow, _locks(), SETTINGS).reconcile()
    assert marked.run is not None and marked.run.core_started_at is not None
    assert result.failed == 1
    failed_task = state.background_tasks[task.id]
    failed_run = state.agent_runs[run.id]
    assert failed_task.error_code == "worker_lost_after_agent_core"
    assert failed_run.status == AgentRunStatus.FAILED
    assert failed_run.core_started_at is not None


@pytest.mark.parametrize("unsafe", ["snapshot", "result", "trace", "attempt"])
def test_agent_pre_core_recovery_requires_all_safety_evidence(unsafe):
    state = InMemoryPersistence()
    uow, _, snapshot, run, task = _running_agent(state)
    if unsafe == "snapshot":
        state.snapshots[snapshot.id] = replace(snapshot, status="failed")
    elif unsafe == "result":
        state.agent_runs[run.id] = replace(run, result="partial")
    elif unsafe == "trace":
        with uow.transaction():
            uow.trace_events.create(run.id, 0, "final")
    else:
        state.background_tasks[task.id] = replace(
            state.background_tasks[task.id], attempt_count=2,
        )
    result = StaleRunningReconciler(uow, _locks(), SETTINGS).reconcile()
    assert result.failed == 1
    assert state.background_tasks[task.id].status == BackgroundTaskStatus.FAILED
    assert state.agent_runs[run.id].status == AgentRunStatus.FAILED


def test_active_project_lock_defers_stale_recovery():
    state = InMemoryPersistence()
    uow, project, _, _, task = _running_agent(state)
    locks = _locks()
    with locks.operation(project.id):
        result = StaleRunningReconciler(uow, locks, SETTINGS).reconcile()
    assert result.deferred == 1
    assert state.background_tasks[task.id].status == BackgroundTaskStatus.RUNNING


def test_real_redis_project_lock_defers_stale_scanner():
    redis_url = os.environ.get("TEST_REDIS_URL") or os.environ.get("REDIS_URL")
    if not redis_url:
        pytest.skip("TEST_REDIS_URL or REDIS_URL is required for Redis integration")
    client = Redis.from_url(redis_url, decode_responses=True)
    try:
        client.ping()
    except (RedisError, OSError) as exc:
        pytest.skip(f"Redis integration unavailable: {type(exc).__name__}")
    state = InMemoryPersistence()
    uow, project, _, _, task = _running_agent(state)
    locks = ProjectOperationLocks(
        RedisProjectLockBackend(client=client), ttl_seconds=10,
        renewal_interval_seconds=2,
    )
    with locks.operation(project.id):
        result = StaleRunningReconciler(uow, locks, SETTINGS).reconcile()
    assert result.deferred == 1
    assert state.background_tasks[task.id].status == BackgroundTaskStatus.RUNNING


def test_stale_owner_cannot_write_trace_or_terminal_after_scanner():
    state = InMemoryPersistence()
    uow, _, _, run, task = _running_agent(state)
    old_token = task.execution_token
    assert old_token is not None
    with uow.transaction():
        uow.agent_runs.mark_core_started(run.id)
    StaleRunningReconciler(uow, _locks(), SETTINGS).reconcile()
    with uow.transaction():
        assert uow.background_tasks.guard_execution(task.id, old_token) is None
        assert uow.background_tasks.mark_completed(
            task.id, execution_token=old_token,
        ) is None
    events = uow.trace_events.list_for_run(run.id)
    assert [event.event_type for event in events] == ["run_started", "run_failed"]
    assert events[-1].payload["event_payload"]["trace_complete"] is False
    assert events[-1].payload["event_payload"]["error"]["category"] == "worker_lost"


def test_healthy_running_is_ignored_and_batch_size_is_honored():
    state = InMemoryPersistence()
    uow = InMemoryUnitOfWork(state)
    with uow.transaction():
        healthy = uow.background_tasks.create("health")
        healthy = uow.background_tasks.claim(healthy.id).task
        stale_one = uow.background_tasks.create("unknown-one")
        stale_one = uow.background_tasks.claim(stale_one.id).task
        stale_two = uow.background_tasks.create("unknown-two")
        stale_two = uow.background_tasks.claim(stale_two.id).task
    assert healthy is not None and stale_one is not None and stale_two is not None
    _stale(state, stale_one.id)
    _stale(state, stale_two.id)
    one_at_a_time = replace(SETTINGS, batch_size=1)
    first = StaleRunningReconciler(uow, _locks(), one_at_a_time).reconcile()
    assert first.scanned == 1 and first.failed == 1
    assert state.background_tasks[healthy.id].status == BackgroundTaskStatus.RUNNING
    second = StaleRunningReconciler(uow, _locks(), one_at_a_time).reconcile()
    assert second.scanned == 1 and second.failed == 1


def test_stale_scanner_is_registered_in_celery_beat(monkeypatch):
    monkeypatch.setenv("PYCODE_STALE_RECONCILE_INTERVAL_SECONDS", "19")
    app = create_celery_app("redis://localhost:6379/15")
    app.loader.import_default_modules()
    assert RECONCILE_STALE_RUNNING_CELERY_TASK_NAME in app.tasks
    entry = app.conf.beat_schedule["reconcile-stale-running-background-tasks"]
    assert entry == {
        "task": RECONCILE_STALE_RUNNING_CELERY_TASK_NAME, "schedule": 19.0,
    }


def test_stale_agent_return_cannot_overwrite_reconciled_terminal_state(
    client, ready_url, application, agent_worker, monkeypatch,
):
    accepted = client.post(
        "/api/v1/agent-runs",
        json={
            "project_id": ready_url.rsplit("/", 1)[-1],
            "task": "analyze safely",
        },
    )
    assert accepted.status_code == 202
    run_id = UUID(accepted.json()["run_id"])
    task_id = UUID(accepted.json()["task_id"])

    def stale_during_core(root, task_text):
        state = application.state.persistence
        running_task = state.background_tasks[task_id]
        assert running_task.execution_token is not None
        state.background_tasks[task_id] = replace(
            running_task, lease_expires_at=EXPIRED,
        )
        recovery_uow = InMemoryUnitOfWork(state)
        with recovery_uow.transaction():
            failed = recovery_uow.background_tasks.fail_if_stale(
                task_id, running_task.execution_token,
                error_code="worker_lost_after_agent_core",
                error_message="Worker was lost after Agent Core started.",
            )
            assert failed is not None
            recovery_uow.agent_runs.fail(
                run_id, "Worker was lost after Agent Core started.",
            )
        return SimpleNamespace(ok=True, answer="stale answer", trace=[])

    monkeypatch.setattr(application.state.adapter, "run_agent", stale_during_core)
    returned = agent_worker.execute(task_id)
    persisted_task = application.state.persistence.background_tasks[task_id]
    persisted_run = application.state.persistence.agent_runs[run_id]
    assert returned.status == AgentRunStatus.FAILED
    assert persisted_task.status == BackgroundTaskStatus.FAILED
    assert persisted_task.error_code == "worker_lost_after_agent_core"
    assert persisted_run.status == AgentRunStatus.FAILED
    assert persisted_run.result is None
    assert [
        event for event in application.state.persistence.trace_events.values()
        if event.run_id == run_id
    ] == []


def test_repository_recovery_reuses_exact_bound_snapshot_without_synchronize(
    ready_url, application, monkeypatch,
):
    project_id = UUID(ready_url.rsplit("/", 1)[-1])
    state = application.state.persistence
    project = state.projects[project_id]
    snapshot = state.snapshots[project.current_snapshot_id]
    original_snapshot_id = snapshot.id
    original_commit = snapshot.commit_sha
    state.snapshots[snapshot.id] = replace(snapshot, status="indexing")
    state.projects[project.id] = replace(
        project, status=ProjectStatus.INDEXING, current_snapshot_id=None,
    )
    uow = InMemoryUnitOfWork(state)
    with uow.transaction():
        task = uow.background_tasks.create(
            REPOSITORY_INDEX_TASK_TYPE, project_id=project.id,
            snapshot_id=snapshot.id,
        )
        task = uow.background_tasks.claim(task.id).task
    assert task is not None and task.execution_token is not None

    monkeypatch.setattr(
        application.state.workspace, "synchronize",
        lambda project: (_ for _ in ()).throw(
            AssertionError("recovery must not resolve remote HEAD"),
        ),
    )
    checkout_calls = []
    real_checkout = application.state.workspace.checkout_exact

    def checkout_exact(recovery_project, commit_sha):
        checkout_calls.append(commit_sha)
        return real_checkout(recovery_project, commit_sha)

    monkeypatch.setattr(application.state.workspace, "checkout_exact", checkout_exact)
    projects = build_project_service(
        uow, adapter=application.state.adapter,
        workspace=application.state.workspace,
        artifacts=application.state.artifacts,
        operations=application.state.project_operations,
    )

    class Ownership:
        def ensure_owned(self):
            with uow.transaction():
                if uow.background_tasks.guard_execution(
                    task.id, task.execution_token,
                ) is None:
                    raise BackendError(
                        "worker_execution_lost", "Execution ownership was lost.",
                    )

    recovered = projects.recover_index_for_snapshot(
        project.id, snapshot.id, task_id=task.id,
        execution_token=task.execution_token, execution=Ownership(),
    )
    assert recovered.current_snapshot_id == original_snapshot_id
    assert state.snapshots[original_snapshot_id].commit_sha == original_commit
    assert state.snapshots[original_snapshot_id].status == "ready"
    assert checkout_calls == [original_commit]
