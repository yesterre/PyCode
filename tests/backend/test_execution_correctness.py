from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from redis import Redis
from redis.exceptions import RedisError

from backend.app.core.errors import BackendError
from backend.app.core.models import (
    AgentRunStatus, BackgroundTaskClaimStatus, BackgroundTaskStatus,
)
from backend.app.infrastructure.projects import (
    InMemoryProjectLockBackend,
    ProjectOperationLocks,
    RedisProjectLockBackend,
)
from backend.app.repositories import InMemoryPersistence, InMemoryUnitOfWork
from backend.app.runtime import project_operation_locks_from_environment
from backend.app.services.background_tasks import (
    REPOSITORY_INDEX_TASK_TYPE,
    BackgroundTaskService,
)


def _create_task(state: InMemoryPersistence, task_type: str = "health"):
    uow = InMemoryUnitOfWork(state)
    with uow.transaction():
        return uow.background_tasks.create(task_type)


def test_memory_atomic_claim_has_one_winner_and_one_running_loser():
    state = InMemoryPersistence()
    task = _create_task(state)
    barrier = Barrier(2)

    def claim():
        uow = InMemoryUnitOfWork(state)
        barrier.wait(timeout=5)
        with uow.transaction():
            return uow.background_tasks.claim(task.id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: claim(), range(2)))

    assert sorted(claim.status for claim in claims) == [
        BackgroundTaskClaimStatus.ALREADY_RUNNING,
        BackgroundTaskClaimStatus.CLAIMED,
    ]
    persisted = state.background_tasks[task.id]
    assert persisted.status == BackgroundTaskStatus.RUNNING
    assert persisted.attempt_count == 1


def test_claim_distinguishes_terminal_and_not_found():
    state = InMemoryPersistence()
    completed = _create_task(state)
    failed = _create_task(state)
    uow = InMemoryUnitOfWork(state)
    with uow.transaction():
        uow.background_tasks.mark_running(completed.id)
        uow.background_tasks.mark_completed(completed.id)
        uow.background_tasks.mark_running(failed.id)
        uow.background_tasks.mark_failed(
            failed.id, error_code="expected", error_message="Expected failure.",
        )
        completed_claim = uow.background_tasks.claim(completed.id)
        failed_claim = uow.background_tasks.claim(failed.id)
        missing_claim = uow.background_tasks.claim(uuid4())

    assert completed_claim.status == BackgroundTaskClaimStatus.TERMINAL
    assert failed_claim.status == BackgroundTaskClaimStatus.TERMINAL
    assert missing_claim.status == BackgroundTaskClaimStatus.NOT_FOUND
    assert missing_claim.task is None


def test_repository_index_duplicate_running_and_terminal_deliveries_are_noops():
    state = InMemoryPersistence()
    uow = InMemoryUnitOfWork(state)
    with uow.transaction():
        project = uow.projects.create("demo", "https://github.com/example/demo.git")
        snapshot = uow.snapshots.upsert(
            project.id, "a" * 40,
            index_artifact_path="artifacts/snapshot/index.json",
            graph_artifact_path="artifacts/snapshot/code_graph.json",
            summary=SimpleNamespace(file_count=1, node_count=1, edge_count=0),
        )
        task = uow.background_tasks.create(
            REPOSITORY_INDEX_TASK_TYPE, project_id=project.id,
        )

    entered, release = Event(), Event()
    calls = []

    def index(project_id):
        calls.append(project_id)
        entered.set()
        assert release.wait(5)
        return SimpleNamespace(current_snapshot_id=snapshot.id)

    first_service = BackgroundTaskService(InMemoryUnitOfWork(state))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first_service.execute_repository_index, task.id, index)
        assert entered.wait(5)
        duplicate = BackgroundTaskService(
            InMemoryUnitOfWork(state),
        ).execute_repository_index(task.id, index)
        assert duplicate.status == BackgroundTaskStatus.RUNNING
        assert duplicate.attempt_count == 1
        assert calls == [project.id]
        release.set()
        completed = future.result(timeout=5)

    after_terminal = BackgroundTaskService(
        InMemoryUnitOfWork(state),
    ).execute_repository_index(task.id, index)
    assert completed.status == after_terminal.status == BackgroundTaskStatus.COMPLETED
    assert after_terminal.attempt_count == 1
    assert calls == [project.id]


def test_repository_index_duplicate_failed_delivery_is_noop():
    state = InMemoryPersistence()
    uow = InMemoryUnitOfWork(state)
    with uow.transaction():
        project = uow.projects.create("demo", "https://github.com/example/demo.git")
        task = uow.background_tasks.create(
            REPOSITORY_INDEX_TASK_TYPE, project_id=project.id,
        )
        uow.background_tasks.mark_running(task.id)
        failed = uow.background_tasks.mark_failed(
            task.id, error_code="expected", error_message="Expected failure.",
        )

    called = False

    def forbidden(_):
        nonlocal called
        called = True
        raise AssertionError("duplicate delivery executed indexing")

    duplicate = BackgroundTaskService(
        InMemoryUnitOfWork(state),
    ).execute_repository_index(task.id, forbidden)
    assert duplicate == failed
    assert called is False


def test_agent_duplicate_running_and_completed_delivery_calls_core_once(
    client, ready_url, application, agent_worker, monkeypatch,
):
    accepted = client.post(
        "/api/v1/agent-runs",
        json={
            "project_id": ready_url.rsplit("/", 1)[-1],
            "task": "analyze entry",
        },
    ).json()
    task_id = UUID(accepted["task_id"])
    run_id = UUID(accepted["run_id"])
    entered, release = Event(), Event()
    calls = []

    def run_agent(root, task):
        calls.append((root, task))
        entered.set()
        assert release.wait(5)
        return SimpleNamespace(ok=True, answer="done", trace=SimpleNamespace(events=[]))

    monkeypatch.setattr(application.state.adapter, "run_agent", run_agent)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(agent_worker.execute, task_id)
        assert entered.wait(5)
        duplicate_running = agent_worker.execute(task_id)
        assert duplicate_running.status == AgentRunStatus.RUNNING
        assert len(calls) == 1
        release.set()
        completed = future.result(timeout=5)

    duplicate_completed = agent_worker.execute(task_id)
    assert completed.status == duplicate_completed.status == AgentRunStatus.COMPLETED
    assert duplicate_completed.id == run_id
    assert len(calls) == 1
    background = application.state.persistence.background_tasks[task_id]
    assert background.status == BackgroundTaskStatus.COMPLETED
    assert background.attempt_count == 1


def test_agent_duplicate_failed_delivery_preserves_terminal_state(
    client, ready_url, application, agent_worker,
):
    accepted = client.post(
        "/api/v1/agent-runs",
        json={
            "project_id": ready_url.rsplit("/", 1)[-1],
            "task": "analyze entry",
        },
    ).json()
    task_id = UUID(accepted["task_id"])
    run_id = UUID(accepted["run_id"])
    uow = InMemoryUnitOfWork(application.state.persistence)
    with uow.transaction():
        claim = uow.background_tasks.claim(task_id)
        assert claim.status == BackgroundTaskClaimStatus.CLAIMED
        uow.agent_runs.mark_running(run_id)
        uow.agent_runs.fail(run_id, "Expected failure.")
        uow.background_tasks.mark_failed(
            task_id, error_code="expected", error_message="Expected failure.",
        )

    duplicate = agent_worker.execute(task_id)
    assert duplicate.status == AgentRunStatus.FAILED
    assert duplicate.error_message == "Expected failure."
    assert application.state.persistence.background_tasks[task_id].attempt_count == 1


def test_agent_claim_rolls_back_when_linked_state_is_inconsistent(
    client, ready_url, application, agent_worker,
):
    accepted = client.post(
        "/api/v1/agent-runs",
        json={
            "project_id": ready_url.rsplit("/", 1)[-1],
            "task": "analyze entry",
        },
    ).json()
    task_id = UUID(accepted["task_id"])
    run_id = UUID(accepted["run_id"])
    uow = InMemoryUnitOfWork(application.state.persistence)
    with uow.transaction():
        uow.agent_runs.mark_running(run_id)

    with pytest.raises(BackendError) as failure:
        agent_worker.execute(task_id)

    assert failure.value.code == "background_task_consistency_error"
    assert application.state.persistence.background_tasks[task_id].status == "queued"
    assert application.state.persistence.background_tasks[task_id].attempt_count == 0


def test_project_lock_same_project_excludes_and_different_projects_do_not():
    backend = InMemoryProjectLockBackend()
    first = ProjectOperationLocks(backend, ttl_seconds=1, renewal_interval_seconds=.1)
    second = ProjectOperationLocks(backend, ttl_seconds=1, renewal_interval_seconds=.1)
    project_a, project_b = uuid4(), uuid4()

    with first.operation(project_a):
        with pytest.raises(BackendError) as busy:
            with second.operation(project_a):
                pass
        assert busy.value.code == "project_busy"
        with second.operation(project_b):
            pass

    assert first.key_for(project_a) == second.key_for(project_a)
    assert first.key_for(project_a).startswith("pycode:lock:project:")


def test_fastapi_and_worker_factories_share_the_redis_project_namespace(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    api_locks = project_operation_locks_from_environment()
    worker_locks = project_operation_locks_from_environment()
    project_id = uuid4()

    assert isinstance(api_locks.backend, RedisProjectLockBackend)
    assert isinstance(worker_locks.backend, RedisProjectLockBackend)
    assert api_locks.key_for(project_id) == worker_locks.key_for(project_id)


def test_project_lock_owner_expiry_release_and_renew_semantics():
    current = [0.0]
    backend = InMemoryProjectLockBackend(clock=lambda: current[0])
    key = ProjectOperationLocks.key_for(uuid4())

    assert backend.acquire(key, "old", 100)
    assert not backend.release(key, "stranger")
    assert not backend.renew(key, "stranger", 100)
    assert backend.renew(key, "old", 200)
    current[0] = .15
    assert backend.owns(key, "old")
    current[0] = .25
    assert backend.acquire(key, "new", 100)
    assert not backend.release(key, "old")
    assert backend.owns(key, "new")


def test_project_lock_renewal_thread_keeps_owner_alive():
    backend = InMemoryProjectLockBackend()
    locks = ProjectOperationLocks(
        backend, ttl_seconds=.2, renewal_interval_seconds=.04,
    )
    with locks.operation(uuid4()) as handle:
        time.sleep(.35)
        assert backend.owns(handle.key, handle.token)
        assert handle.lost is False


def test_project_lock_loss_is_detected_and_old_owner_cannot_delete_new_lock():
    backend = InMemoryProjectLockBackend()
    locks = ProjectOperationLocks(
        backend, ttl_seconds=1, renewal_interval_seconds=.2,
    )
    replacement = "replacement"

    with pytest.raises(BackendError) as failure:
        with locks.operation(uuid4()) as handle:
            assert backend.release(handle.key, handle.token)
            assert backend.acquire(handle.key, replacement, 1000)

    assert failure.value.code == "project_lock_lost"
    assert backend.owns(handle.key, replacement)


def test_real_redis_lock_uses_atomic_owner_scripts():
    redis_url = os.environ.get("TEST_REDIS_URL") or os.environ.get("REDIS_URL")
    if not redis_url:
        pytest.skip("TEST_REDIS_URL or REDIS_URL is required for Redis integration")
    client = Redis.from_url(redis_url, decode_responses=True)
    try:
        client.ping()
    except (RedisError, OSError) as exc:
        pytest.skip(f"Redis integration unavailable: {type(exc).__name__}")

    backend = RedisProjectLockBackend(client=client)
    key = ProjectOperationLocks.key_for(uuid4())
    try:
        assert backend.acquire(key, "old", 80)
        assert not backend.release(key, "stranger")
        assert not backend.renew(key, "stranger", 500)
        assert backend.renew(key, "old", 80)
        time.sleep(.12)
        assert backend.acquire(key, "new", 1000)
        assert not backend.release(key, "old")
        assert backend.owns(key, "new")
    finally:
        backend.release(key, "new")
