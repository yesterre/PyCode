from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from queue import Empty
import subprocess
import site
import socket
import sys
from threading import Barrier
import time
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from celery import current_app
from redis import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from backend.app.config import QueuedReconciliationSettings
from backend.app.core.models import IndexSummary, ProjectStatus
from backend.app.db.models import BackgroundTaskORM
from backend.app.infrastructure.celery import CeleryTaskDispatcher, create_celery_app
from backend.app.infrastructure.projects import ProjectOperationLocks, RedisProjectLockBackend
from backend.app.services.queued_reconciliation import QueuedTaskReconciler
from backend.app.services.stale_reconciliation import StaleRunningReconciler
from test_b01_users import uow_factory
from test_b02_auth import auth_context
from test_b04_project_derived_auth import resources
from test_b05_async_auth import counts, submit
from test_b07_worker_isolation import accept_index, mutate, ready, workers
from test_postgresql import postgresql_engine


def prepared(r):
    with r.context.uow_factory() as uow, uow.transaction():
        for project in [r.own, r.sibling, r.foreign]:
            snapshot = uow.snapshots.upsert(project, "a" * 40, index_artifact_path="unused/index.json",
                                           graph_artifact_path="unused/graph.json", summary=IndexSummary(1, 1, 0))
            uow.projects.set_current_snapshot(project, snapshot.id)
            uow.projects.set_status(project, ProjectStatus.READY)


def expire(r, task_id):
    with r.context.uow_factory() as uow, uow.transaction():
        mutate(uow, "background_tasks", BackgroundTaskORM, task_id,
               lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=30))


@pytest.fixture
def real_redis():
    url = os.environ.get("TEST_REDIS_URL") or os.environ.get("REDIS_URL")
    if not url:
        pytest.skip("TEST_REDIS_URL or REDIS_URL is required for B12 drills")
    client = Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1, retry=Retry(NoBackoff(), 0))
    client.ping()
    keys = set()
    try:
        yield SimpleNamespace(url=url, client=client, keys=keys)
    finally:
        if keys:
            client.delete(*keys)
        client.close()


def eventually(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.03)
    pytest.fail("Fault drill did not reach its bounded checkpoint")


def task_state(r, identifier):
    with r.context.uow_factory() as uow, uow.transaction():
        return uow.background_tasks.get(identifier)


def reconcile(r, operations=None):
    with r.context.uow_factory() as uow:
        return StaleRunningReconciler(uow, operations or r.context.app.state.project_operations).reconcile()


def republish(r, dispatcher):
    with r.context.uow_factory() as uow:
        return QueuedTaskReconciler(uow, dispatcher, QueuedReconciliationSettings(),
            clock=lambda: datetime.now(timezone.utc) + timedelta(minutes=1)).run()


@pytest.mark.parametrize("association", ["project", "snapshot"])
@pytest.mark.parametrize("victim_state", ["queued", "running", "post_core"])
def test_stale_scanner_cannot_read_trace_or_mutate_mismatched_run(resources, monkeypatch,
                                                               association, victim_state):
    r = resources
    prepared(r)
    with r.context.uow_factory() as uow, uow.transaction():
        if association == "project":
            project, snapshot = r.foreign, r.foreign_snapshot.id
        else:
            project = r.own
            snapshot = uow.snapshots.upsert(r.own, "b" * 40, index_artifact_path="unused/other.json",
                graph_artifact_path="unused/other-graph.json", summary=IndexSummary(1, 1, 0)).id
        victim = uow.agent_runs.create_queued(project, snapshot, task="private victim", run_type="agent", model=None)
        if victim_state != "queued":
            victim = uow.agent_runs.mark_running(victim.id)
        if victim_state == "post_core":
            victim = uow.agent_runs.mark_core_started(victim.id).run
        task = uow.background_tasks.create("agent_run", project_id=r.own,
                                         snapshot_id=r.snapshot.id, agent_run_id=victim.id)
        task = uow.background_tasks.claim(task.id).task
    expire(r, task.id)
    trace_reads = []
    with r.context.uow_factory() as uow:
        repository_class = type(uow.trace_events)
        original = repository_class.list_for_run

    def trace_read(repository, run_id):
        trace_reads.append(run_id)
        return original(repository, run_id)

    monkeypatch.setattr(repository_class, "list_for_run", trace_read)
    with r.context.uow_factory() as uow:
        result = StaleRunningReconciler(uow, r.context.app.state.project_operations).reconcile()
    with r.context.uow_factory() as uow, uow.transaction():
        persisted = uow.background_tasks.get(task.id)
        assert result.failed == 1 and persisted.status == "failed"
        assert persisted.error_code == "worker_execution_lost"
        assert uow.agent_runs.get(victim.id) == victim
        assert uow.projects.get(r.own).current_snapshot_id == r.snapshot.id
    assert victim.id not in trace_reads


@pytest.fixture
def wire_broker(real_redis, monkeypatch):
    """Real Kombu/Redis transport, isolated queue and key prefix, no worker daemon."""
    previous = current_app._get_current_object()
    app = create_celery_app(real_redis.url)
    prefix = f"b12:{uuid4().hex}:"
    queue = "b12-" + uuid4().hex
    app.conf.broker_transport_options = {**app.conf.broker_transport_options, "global_keyprefix": prefix}
    original = app.send_task
    state = SimpleNamespace(fault=None, sends=0, app=app)

    def publish(name, *, args, queue):
        assert queue == "pycode"  # Preserve production dispatch; route only this test to isolation.
        if state.fault == "before":
            raise ConnectionError("injected transport failure before send")
        result = original(name, args=args, queue=state.queue, retry=False)
        state.sends += 1
        if state.fault == "after":
            raise ConnectionError("injected lost publish acknowledgement")
        return result

    def messages():
        items = []
        with app.connection_for_read() as connection:
            simple = connection.SimpleQueue(queue)
            try:
                while True:
                    try:
                        message = simple.get(block=False)
                    except Empty:
                        return items
                    items.append((message.headers["task"], message.payload[0]))
                    message.ack()
            finally:
                simple.close()

    state.queue, state.messages = queue, messages
    monkeypatch.setattr(app, "send_task", publish)
    state.dispatcher = CeleryTaskDispatcher(lambda: app)
    try:
        yield state
    finally:
        app.close()
        previous.set_current()
        keys = list(real_redis.client.scan_iter(match=prefix + "*"))
        assert all(key.startswith(prefix.encode()) for key in keys)
        if keys:
            real_redis.client.delete(*keys)


@pytest.mark.parametrize("fault", ["before", "after"])
def test_publish_failure_replay_republish_and_duplicate_delivery_preserve_identity(
    resources, workers, wire_broker, monkeypatch, fault,
):
    r, broker = resources, wire_broker
    ready(r, workers)
    r.context.app.state.task_dispatcher = broker.dispatcher
    broker.fault = fault
    before = counts(r.context)
    response = submit(r.context, r.own, r.headers, "b12-publish")
    assert response.status_code == 503 and response.json()["detail"]["code"] == "task_queue_unavailable"
    replay = submit(r.context, r.own, r.headers, "b12-publish")
    assert replay.status_code == 202, replay.text
    task_id, run_id = UUID(replay.json()["task_id"]), UUID(replay.json()["run_id"])
    persisted = task_state(r, task_id)
    assert persisted.status == "queued" and persisted.attempt_count == 0
    assert persisted.enqueue_confirmed_at is None and persisted.enqueue_attempt_count == 1
    assert counts(r.context)[:2] == (before[0] + 1, before[1] + 1)
    broker.fault = None
    result = republish(r, broker.dispatcher)
    assert result.confirmed_count == 1
    messages = broker.messages()
    assert len(messages) == (2 if fault == "after" else 1)
    assert all(name == "pycode.tasks.run_agent" and args == [str(task_id)] for name, args in messages)
    calls, original = [], r.context.app.state.adapter.run_agent

    def count_core(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(r.context.app.state.adapter, "run_agent", count_core)
    for _ in messages + [messages[0]]:
        workers.agent(str(task_id))
    with r.context.uow_factory() as uow, uow.transaction():
        task, run = uow.background_tasks.get(task_id), uow.agent_runs.get(run_id)
        assert task.status == run.status == "completed"
        assert task.attempt_count == 1 and task.enqueue_attempt_count == 2
        assert task.agent_run_id == run.id and task.snapshot_id == run.snapshot_id == r.snapshot.id
        trace = uow.trace_events.list_for_run(run_id)
        assert trace and len({event.sequence for event in trace}) == len(trace)
    assert calls == [1]
    assert counts(r.context)[:2] == (before[0] + 1, before[1] + 1)
    assert submit(r.context, r.own, r.headers, "b12-publish").json() == {
        "task_id": str(task_id), "run_id": str(run_id), "status": "completed",
    }
    assert r.context.client.get(f"/api/v1/agent-runs/{run_id}", headers=r.other_headers).status_code == 404


def test_index_execution_crash_recovers_bound_snapshot_without_remote_head(resources, workers, monkeypatch):
    r = resources
    identifier = UUID(accept_index(r.context, r.own, r.headers))

    class ProcessCrash(BaseException):
        pass

    with monkeypatch.context() as patch:
        def crash(*args, **kwargs):
            raise ProcessCrash()
        patch.setattr(r.context.app.state.adapter, "index", crash)
        with pytest.raises(ProcessCrash):
            workers.index(str(identifier))
    old = task_state(r, identifier)
    assert old.status == "running" and old.snapshot_id is not None
    expire(r, identifier)
    assert reconcile(r).requeued == 1
    assert republish(r, r.context.app.state.task_dispatcher).confirmed_count == 1

    def no_remote_head(*args):
        pytest.fail("Recovery must use its persisted Snapshot, not remote HEAD")

    monkeypatch.setattr(r.context.app.state.workspace, "synchronize", no_remote_head)
    workers.index(str(identifier))
    with r.context.uow_factory() as uow, uow.transaction():
        current = uow.background_tasks.get(identifier)
        snapshot = uow.snapshots.get(current.snapshot_id)
        assert current.status == "completed" and current.attempt_count == 2
        assert current.snapshot_id == old.snapshot_id
        assert snapshot.status == "ready" and snapshot.commit_sha == "a" * 40
        assert uow.projects.get(r.own).current_snapshot_id == snapshot.id
        assert uow.background_tasks.mark_completed(identifier, execution_token=old.execution_token) is None


def test_redis_outage_and_live_lock_defer_then_competing_scanners_requeue_once(resources, real_redis):
    r = resources
    prepared(r)
    with r.context.uow_factory() as uow, uow.transaction():
        run = uow.agent_runs.create_queued(r.own, r.snapshot.id, task="recover", run_type="agent", model=None)
        task = uow.background_tasks.create("agent_run", project_id=r.own, snapshot_id=r.snapshot.id,
                                           agent_run_id=run.id)
        task = uow.background_tasks.claim(task.id).task
        run = uow.agent_runs.mark_running(run.id)
    expire(r, task.id)
    before = task_state(r, task.id)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        broken = Redis(host="127.0.0.1", port=reserved.getsockname()[1], socket_timeout=0.1,
                       socket_connect_timeout=0.1, retry=Retry(NoBackoff(), 0))
        try:
            operations = ProjectOperationLocks(RedisProjectLockBackend(client=broken))
            assert reconcile(r, operations).deferred == 1
            assert task_state(r, task.id) == before
        finally:
            broken.close()
    operations = ProjectOperationLocks(RedisProjectLockBackend(client=real_redis.client),
                                       ttl_seconds=1, renewal_interval_seconds=0.15)
    real_redis.keys.add(operations.key_for(r.own))
    with operations.operation(r.own):
        assert reconcile(r, operations).deferred == 1
        assert task_state(r, task.id) == before
    barrier = Barrier(2)
    def scan(_):
        barrier.wait(timeout=10)
        return reconcile(r, operations)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(scan, range(2)))
    assert sum(result.requeued for result in results) == 1
    with r.context.uow_factory() as uow, uow.transaction():
        current = uow.background_tasks.get(task.id)
        assert current.status == uow.agent_runs.get(run.id).status == "queued"
        assert current.agent_run_id == run.id and current.snapshot_id == r.snapshot.id
        assert current.execution_token is None
        assert uow.background_tasks.heartbeat(task.id, task.execution_token, lease_ttl_seconds=2) is None
        assert uow.background_tasks.mark_completed(task.id, execution_token=task.execution_token) is None
    assert republish(r, r.context.app.state.task_dispatcher).confirmed_count == 1
    assert r.context.app.state.task_dispatcher.agent_run_task_ids == [task.id]


@pytest.mark.parametrize("uow_factory", ["postgresql"], indirect=True)
@pytest.mark.parametrize("checkpoint", ["claim", "pre_core", "post_core"])
def test_killed_worker_process_recovers_only_before_core(
    uow_factory, resources, workers, real_redis, source_repository, tmp_path, monkeypatch, checkpoint,
):
    r = resources
    ready(r, workers)
    accepted = submit(r.context, r.own, r.headers, "b12-killed-worker")
    assert accepted.status_code == 202, accepted.text
    task_id, run_id = UUID(accepted.json()["task_id"]), UUID(accepted.json()["run_id"])
    signal, effect = tmp_path / "checkpoint.json", tmp_path / "external-effect.txt"
    environment = {**os.environ, "B12_TEST_REDIS_URL": real_redis.url}
    # Windows venv python.exe is a redirector. Launch the base interpreter
    # directly with this venv's packages so Popen.pid is the actual Worker PID.
    interpreter = getattr(sys, "_base_executable", sys.executable) if os.name == "nt" else sys.executable
    environment["PYTHONPATH"] = os.pathsep.join([*site.getsitepackages(), environment.get("PYTHONPATH", "")])
    key = ProjectOperationLocks.key_for(r.own)
    real_redis.keys.add(key)
    child = subprocess.Popen([
        interpreter, str(Path(__file__).with_name("b12_worker_probe.py")),
        "--task", str(task_id), "--checkpoint", checkpoint,
        "--workspace", str(r.context.app.state.workspace.root), "--source", str(source_repository),
        "--signal", str(signal), "--effect", str(effect),
    ], cwd=Path(__file__).resolve().parents[2], env=environment,
       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        def reached():
            assert child.poll() is None, child.communicate(timeout=5)
            if not signal.exists():
                return False
            try:
                return json.loads(signal.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return False
        handshake = eventually(reached, timeout=30)
        assert handshake == {"stage": checkpoint, "pid": child.pid, "task": str(task_id)}
        owned = task_state(r, task_id)
        assert owned.status == "running" and owned.worker_id == f"b12-probe:{child.pid}"
        with r.context.uow_factory() as uow, uow.transaction():
            checkpoint_run = uow.agent_runs.get(run_id)
            assert checkpoint_run.status == ("queued" if checkpoint == "claim" else "running")
            assert (checkpoint_run.core_started_at is not None) == (checkpoint == "post_core")
        if checkpoint != "claim":
            eventually(lambda: task_state(r, task_id).last_heartbeat_at > owned.last_heartbeat_at)
            assert real_redis.client.exists(key)
        child.kill()  # Only the Popen child owned by this test; no external Worker is stopped.
        child.communicate(timeout=10)
        assert child.returncode != 0
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)
    eventually(lambda: not real_redis.client.exists(key), timeout=5)
    eventually(lambda: task_state(r, task_id).lease_expires_at <= datetime.now(timezone.utc), timeout=5)
    operations = ProjectOperationLocks(RedisProjectLockBackend(client=real_redis.client),
                                       ttl_seconds=1, renewal_interval_seconds=0.15)
    outcome = reconcile(r, operations)
    with r.context.uow_factory() as uow, uow.transaction():
        task, run = uow.background_tasks.get(task_id), uow.agent_runs.get(run_id)
        assert task.snapshot_id == run.snapshot_id == r.snapshot.id
        assert uow.background_tasks.heartbeat(task_id, owned.execution_token, lease_ttl_seconds=2) is None
        assert uow.background_tasks.mark_completed(task_id, execution_token=owned.execution_token) is None
        if checkpoint == "pre_core":
            assert outcome.requeued == 1 and task.status == run.status == "queued"
            assert run.core_started_at is None and task.execution_token is None
        else:
            assert outcome.failed == 1 and task.status == run.status == "failed"
            assert task.error_code == ("worker_lost_after_agent_core" if checkpoint == "post_core" else "worker_execution_lost")
    calls, original = [], r.context.app.state.adapter.run_agent
    def count_core(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(r.context.app.state.adapter, "run_agent", count_core)
    if checkpoint == "pre_core":
        assert republish(r, r.context.app.state.task_dispatcher).confirmed_count == 1
    workers.agent(str(task_id))
    workers.agent(str(task_id))
    with r.context.uow_factory() as uow, uow.transaction():
        task, run = uow.background_tasks.get(task_id), uow.agent_runs.get(run_id)
        assert task.status == run.status == ("completed" if checkpoint == "pre_core" else "failed")
        assert task.attempt_count == (2 if checkpoint == "pre_core" else 1)
        assert uow.projects.get(r.own).current_snapshot_id == r.snapshot.id
        snapshot = uow.snapshots.get(r.snapshot.id)
        if checkpoint != "claim":
            assert uow.trace_events.list_for_run(run_id)
    assert calls == ([1] if checkpoint == "pre_core" else [])
    r.context.app.state.artifacts.read_ready(r.own, snapshot)
    if checkpoint == "post_core":
        assert effect.read_text(encoding="utf-8") == "entered-core\n"
    else:
        assert not effect.exists()
