from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from threading import Barrier, Lock
from uuid import UUID

import pytest

from backend.app.config import QueuedReconciliationSettings
from backend.app.core.errors import BackendError
from backend.app.core.models import BackgroundTaskStatus, IndexSummary
from backend.app.infrastructure.celery import create_celery_app
from backend.app.repositories import InMemoryPersistence, InMemoryUnitOfWork
from backend.app.services.background_tasks import (
    AGENT_RUN_TASK_TYPE,
    REPOSITORY_INDEX_TASK_TYPE,
    BackgroundTaskService,
)
from backend.app.services.queued_reconciliation import (
    RECONCILE_QUEUED_CELERY_TASK_NAME,
    QueuedTaskReconciler,
)


NOW = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)
SETTINGS = QueuedReconciliationSettings(
    publish_grace_seconds=30,
    interval_seconds=20,
    batch_size=10,
    max_backoff_seconds=60,
)


class Dispatcher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.health_task_ids: list[UUID] = []
        self.repository_index_task_ids: list[UUID] = []
        self.agent_run_task_ids: list[UUID] = []
        self.guard = Lock()

    def _record(self, target: list[UUID], task_id: UUID) -> None:
        with self.guard:
            target.append(task_id)
        if self.fail:
            raise ConnectionError("broker unavailable")

    def enqueue_health(self, task_id: UUID) -> None:
        self._record(self.health_task_ids, task_id)

    def enqueue_repository_index(self, task_id: UUID) -> None:
        self._record(self.repository_index_task_ids, task_id)

    def enqueue_agent_run(self, task_id: UUID) -> None:
        self._record(self.agent_run_task_ids, task_id)


def _project(state: InMemoryPersistence):
    uow = InMemoryUnitOfWork(state)
    with uow.transaction():
        return uow.projects.create("Example", "https://github.com/example/demo.git")


def _queued_task(
    state: InMemoryPersistence, task_type: str = REPOSITORY_INDEX_TASK_TYPE,
    *, due_at: datetime = NOW,
):
    uow = InMemoryUnitOfWork(state)
    with uow.transaction():
        project = uow.projects.create("Example", "https://github.com/example/demo.git")
        return uow.background_tasks.create(
            task_type, project_id=project.id, next_enqueue_attempt_at=due_at,
        )


def test_initial_publish_success_persists_confirmation_without_business_attempt():
    state = InMemoryPersistence()
    project = _project(state)
    dispatcher = Dispatcher()
    service = BackgroundTaskService(
        InMemoryUnitOfWork(state), dispatcher, SETTINGS, clock=lambda: NOW,
    )

    task = service.create_repository_index_task(project.id)

    assert dispatcher.repository_index_task_ids == [task.id]
    assert task.status == BackgroundTaskStatus.QUEUED
    assert task.enqueue_attempt_count == 1
    assert task.last_enqueue_attempt_at == NOW
    assert task.enqueue_confirmed_at == NOW
    assert task.next_enqueue_attempt_at is None
    assert task.attempt_count == 0


def test_initial_publish_failure_leaves_due_queued_task_for_recovery():
    state = InMemoryPersistence()
    project = _project(state)
    service = BackgroundTaskService(
        InMemoryUnitOfWork(state), Dispatcher(fail=True), SETTINGS,
        clock=lambda: NOW,
    )

    with pytest.raises(BackendError) as error:
        service.create_repository_index_task(project.id)

    assert error.value.code == "task_queue_unavailable"
    task = next(iter(state.background_tasks.values()))
    assert task.status == BackgroundTaskStatus.QUEUED
    assert task.enqueue_attempt_count == 1
    assert task.enqueue_confirmed_at is None
    assert task.next_enqueue_attempt_at == NOW + timedelta(seconds=5)
    assert task.attempt_count == 0


def test_initial_publish_failure_api_returns_503_and_persists_candidate(
    application, client, project_url,
):
    application.state.task_dispatcher = Dispatcher(fail=True)

    response = client.post(project_url + "/index")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "task_queue_unavailable"
    task = next(iter(application.state.persistence.background_tasks.values()))
    assert task.status == BackgroundTaskStatus.QUEUED
    assert task.enqueue_confirmed_at is None
    assert task.next_enqueue_attempt_at is not None
    assert task.attempt_count == 0


def test_reconciler_recovers_repository_task_with_same_id_and_separate_counts():
    state = InMemoryPersistence()
    task = _queued_task(state)
    dispatcher = Dispatcher()
    reconciler = QueuedTaskReconciler(
        InMemoryUnitOfWork(state), dispatcher, SETTINGS, clock=lambda: NOW,
    )

    result = reconciler.run()

    recovered = state.background_tasks[task.id]
    assert result.candidate_count == result.confirmed_count == 1
    assert dispatcher.repository_index_task_ids == [task.id]
    assert recovered.enqueue_attempt_count == 1
    assert recovered.enqueue_confirmed_at == NOW
    assert recovered.attempt_count == 0
    with InMemoryUnitOfWork(state).transaction():
        claimed = InMemoryUnitOfWork(state).background_tasks.claim(task.id).task
    assert claimed is not None
    assert claimed.attempt_count == 1
    assert claimed.enqueue_attempt_count == 1


def test_reconciler_recovers_agent_task_without_creating_another_run_or_task():
    state = InMemoryPersistence()
    uow = InMemoryUnitOfWork(state)
    with uow.transaction():
        project = uow.projects.create("Example", "https://github.com/example/demo.git")
        snapshot = uow.snapshots.upsert(
            project.id, "a" * 40,
            index_artifact_path="index.json", graph_artifact_path="graph.json",
            summary=IndexSummary(1, 2, 1),
        )
        run = uow.agent_runs.create_queued(
            project.id, snapshot.id, task="Analyze", run_type="agent", model=None,
        )
        task = uow.background_tasks.create(
            AGENT_RUN_TASK_TYPE,
            project_id=project.id,
            snapshot_id=snapshot.id,
            agent_run_id=run.id,
            next_enqueue_attempt_at=NOW,
        )
    dispatcher = Dispatcher()

    result = QueuedTaskReconciler(
        InMemoryUnitOfWork(state), dispatcher, SETTINGS, clock=lambda: NOW,
    ).run()

    assert result.confirmed_count == 1
    assert dispatcher.agent_run_task_ids == [task.id]
    assert list(state.agent_runs) == [run.id]
    assert list(state.background_tasks) == [task.id]


def test_failed_reconciliation_keeps_queued_and_applies_bounded_backoff():
    state = InMemoryPersistence()
    task = _queued_task(state)

    result = QueuedTaskReconciler(
        InMemoryUnitOfWork(state), Dispatcher(fail=True), SETTINGS,
        clock=lambda: NOW,
    ).run()

    persisted = state.background_tasks[task.id]
    assert result.failed_count == 1
    assert persisted.status == BackgroundTaskStatus.QUEUED
    assert persisted.enqueue_attempt_count == 1
    assert persisted.next_enqueue_attempt_at == NOW + timedelta(seconds=5)
    assert persisted.attempt_count == 0


def test_repeated_publish_attempts_only_increment_enqueue_counter():
    state = InMemoryPersistence()
    task = _queued_task(state)
    dispatcher = Dispatcher(fail=True)

    first = QueuedTaskReconciler(
        InMemoryUnitOfWork(state), dispatcher, SETTINGS, clock=lambda: NOW,
    ).run()
    second_time = NOW + timedelta(seconds=5)
    second = QueuedTaskReconciler(
        InMemoryUnitOfWork(state), dispatcher, SETTINGS, clock=lambda: second_time,
    ).run()

    persisted = state.background_tasks[task.id]
    assert first.failed_count == second.failed_count == 1
    assert persisted.enqueue_attempt_count == 2
    assert persisted.next_enqueue_attempt_at == second_time + timedelta(seconds=15)
    assert persisted.attempt_count == 0


def test_reconciler_excludes_confirmed_not_due_running_and_terminal_tasks():
    state = InMemoryPersistence()
    tasks = [_queued_task(state, due_at=NOW) for _ in range(5)]
    uow = InMemoryUnitOfWork(state)
    with uow.transaction():
        uow.background_tasks.mark_enqueue_confirmed(tasks[0].id, NOW)
        not_due = state.background_tasks[tasks[1].id]
        state.background_tasks[tasks[1].id] = not_due.__class__(
            **{**not_due.__dict__, "next_enqueue_attempt_at": NOW + timedelta(seconds=1)}
        )
        uow.background_tasks.claim(tasks[2].id)
        uow.background_tasks.claim(tasks[3].id)
        uow.background_tasks.mark_completed(tasks[3].id)
        uow.background_tasks.claim(tasks[4].id)
        uow.background_tasks.mark_failed(
            tasks[4].id, error_code="failed", error_message="failed",
        )

    result = QueuedTaskReconciler(
        InMemoryUnitOfWork(state), Dispatcher(), SETTINGS, clock=lambda: NOW,
    ).run()

    assert result.candidate_count == 0


def test_concurrent_memory_reconcilers_reserve_and_publish_only_once():
    state = InMemoryPersistence()
    task = _queued_task(state)
    dispatcher = Dispatcher()
    barrier = Barrier(2)

    def run():
        barrier.wait()
        return QueuedTaskReconciler(
            InMemoryUnitOfWork(state), dispatcher, SETTINGS, clock=lambda: NOW,
        ).run()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: run(), range(2)))

    assert sum(result.candidate_count for result in results) == 1
    assert dispatcher.repository_index_task_ids == [task.id]
    assert state.background_tasks[task.id].enqueue_attempt_count == 1


def test_reconciler_respects_batch_limit_and_unknown_type_backoff():
    state = InMemoryPersistence()
    supported = _queued_task(state)
    unknown = _queued_task(state, "future_task")
    dispatcher = Dispatcher()
    one_at_a_time = QueuedReconciliationSettings(0, 1, 1, 10)
    reconciler = QueuedTaskReconciler(
        InMemoryUnitOfWork(state), dispatcher, one_at_a_time, clock=lambda: NOW,
    )

    first = reconciler.run()
    second = reconciler.run()

    assert first.candidate_count == 1
    assert second.candidate_count == 1
    assert first.confirmed_count + second.confirmed_count == 1
    assert first.unsupported_count + second.unsupported_count == 1
    assert dispatcher.repository_index_task_ids == [supported.id]
    assert state.background_tasks[unknown.id].next_enqueue_attempt_at > NOW


def test_celery_beat_registration_uses_environment_interval(monkeypatch):
    monkeypatch.setenv("PYCODE_QUEUED_RECONCILE_INTERVAL_SECONDS", "17")

    celery = create_celery_app("redis://localhost:6379/15")

    entry = celery.conf.beat_schedule["reconcile-queued-background-tasks"]
    assert entry == {"task": RECONCILE_QUEUED_CELERY_TASK_NAME, "schedule": 17.0}
    assert "backend.app.tasks.reconciliation" in celery.conf.include


def test_periodic_celery_task_is_a_thin_reconciler_adapter(monkeypatch):
    from backend.app.tasks import reconciliation as task_module

    calls: list[str] = []

    class Reconciler:
        def run(self):
            calls.append("run")

    @contextmanager
    def worker_reconciler():
        calls.append("open")
        yield Reconciler()
        calls.append("close")

    monkeypatch.setattr(task_module, "worker_queued_task_reconciler", worker_reconciler)

    task_module.reconcile_queued_tasks.run()

    assert calls == ["open", "run", "close"]
