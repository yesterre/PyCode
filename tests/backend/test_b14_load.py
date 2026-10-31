"""Small runner checks run normally; the real load experiment is explicitly opt-in."""
from collections import Counter, defaultdict
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import secrets
import subprocess
from uuid import UUID, uuid4

from celery import current_app
import httpx
import pytest
from redis import Redis
from sqlalchemy import text

from backend.app.auth import hash_password
from backend.app.config import ExecutionLeaseSettings, ObservabilitySettings
from backend.app.infrastructure.celery import CeleryTaskDispatcher, create_celery_app
from backend.app.infrastructure.execution import ExecutionOwnership
from backend.app.main import create_app
from backend.app.repositories import SqlAlchemyUnitOfWork
from backend.app.runtime import build_project_service
from backend.app.services.background_tasks import BackgroundTaskService
from backend.app.services.projects import ProjectService
from b09_query_benchmark import PROJECTS, RUNS, environment, seed
from conftest import REPO_URL
from scripts.benchmarks.b14_load import (
    Actor, IndexConsumer, OPERATIONS, OUTPUT, OperationMiddleware, Telemetry, Workload,
    comparisons, container_resources, frozen_list_owned_page, loopback_server, machine, percentile, summarize,
)
from test_postgresql import _safe_test_database_url, _truncate, postgresql_engine


def identifier(prefix, number):
    return UUID(hashlib.md5(f"{prefix}-{number}".encode()).hexdigest())


def test_load_statistics_include_denials_timeouts_and_nearest_rank_percentiles():
    samples = [{"operation": "projects", "status": 200, "ms": float(value)} for value in range(1, 19)]
    samples += [{"operation": "submit", "status": 429, "ms": 19.0}, {"operation": "login", "status": 0, "ms": 20.0}]
    result = summarize(samples, 2)
    assert result["requests_per_second"] == 10
    assert result["operations"]["all"] == {
        "count": 20, "p50_ms": 10, "p95_ms": 19, "statuses": {"200": 18, "429": 1, "0": 1}, "error_rate": .1,
    }
    assert percentile([], .95) is None
    assert len(OPERATIONS) == 20 and OPERATIONS.count("submit") == 1


def seed_load_data(engine, password, run_id):
    users = {}
    index_numbers = list(range(1001, 1021))
    control_numbers = [49 + 50 * (100 + value) for value in range(16)]
    encoded = hash_password(password)
    with engine.begin() as connection:
        seed(connection)
        # Reuse B09's dataset, but give this run private owner UUIDs and real hashes.
        # Thus neither rate keys nor control cleanup touch another run's users.
        for number in range(1, 51):
            old, new = identifier("owner", number), uuid4()
            account = f"benchmark-owner-{number}"
            connection.execute(text("UPDATE users SET account_name=:old WHERE id=:id"), {"old": account + "-old", "id": old})
            connection.execute(text("INSERT INTO users(id,account_name,password_hash) VALUES(:id,:account,:hash)"),
                               {"id": new, "account": account, "hash": encoded})
            connection.execute(text("UPDATE projects SET owner_id=:new WHERE owner_id=:old"), {"new": new, "old": old})
            connection.execute(text("DELETE FROM users WHERE id=:old"), {"old": old})
            users[number] = new
        connection.execute(text("""
            INSERT INTO project_snapshots(id,project_id,commit_sha,status,index_artifact_path,graph_artifact_path,
                file_count,node_count,edge_count)
            SELECT md5(:tag||'-snapshot-'||i)::uuid,md5('project-'||i)::uuid,repeat('b',40),'ready',
                'b14/metadata-only/index.json','b14/metadata-only/code_graph.json',10,20,30
            FROM generate_series(1,:projects) i WHERE (i/50)%4=1
        """), {"tag": run_id, "projects": PROJECTS})
        connection.execute(text("""
            UPDATE projects p SET current_snapshot_id=s.id,status='ready',updated_at='2026-10-30'::timestamptz
            FROM project_snapshots s WHERE s.project_id=p.id
        """))
        connection.execute(text("UPDATE projects SET repo_url=:repo,archived_at=NULL WHERE id=ANY(:ids)"),
                           {"repo": REPO_URL, "ids": [identifier("project", number) for number in index_numbers + control_numbers]})
        connection.execute(text("""
            INSERT INTO background_tasks(id,task_type,status,attempt_count,created_at,started_at,finished_at)
            SELECT md5(:tag||'-health-'||i)::uuid,'health','completed',1,now()-interval '4 seconds',
                now()-interval '2 seconds',now() FROM generate_series(1,5000) i
        """), {"tag": run_id})
        for table in ("users", "projects", "project_snapshots", "agent_runs", "background_tasks"):
            connection.execute(text("ANALYZE " + table))
    return users, index_numbers, control_numbers


def database_stats(engine):
    with engine.connect() as connection:
        connection.execute(text("SELECT pg_stat_clear_snapshot()"))
        return dict(connection.execute(text("""
            SELECT xact_commit,xact_rollback,blks_read,blks_hit,temp_bytes,deadlocks
            FROM pg_stat_database WHERE datname=current_database()
        """)).mappings().one())


@pytest.mark.skipif(os.environ.get("PYCODE_RUN_B14_LOAD") != "1", reason="B14 load experiment is opt-in; use python -m scripts.benchmarks.b14_load")
def test_mixed_http_load(postgresql_engine, application, llm, monkeypatch):
    database_url = _safe_test_database_url()
    redis_url = os.environ.get("TEST_REDIS_URL") or os.environ.get("REDIS_URL")
    assert redis_url, "A real test Redis is required"
    options = json.loads(os.environ["PYCODE_B14_OPTIONS"])
    levels = [int(value) for value in options["concurrency"].split(",")]
    run_id = uuid4().hex
    report = {"run_id": run_id, "options": options, "machine": machine(), "phases": [],
              "cache": "No production history cache: B10 candidate was rejected",
              "workload": dict(Counter(OPERATIONS)),
              "baseline_source_head": "e2434198234f3269b4a4c41002b26c17c4024725",
              "head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
              "transport": "Real loopback HTTP, real Celery/Kombu Redis transport; one in-process trusted index consumer, not prefork",
              "dataset": {"users": 50, "projects": PROJECTS, "history_runs": RUNS, "metadata_snapshots": PROJECTS // 4,
                          "historical_health_tasks": 5000, "list_limit": 50, "history_limit": 20, "history_offset": 1000,
                          "snapshot_scope": "Metadata-only historical list summaries; the 20 measured index projects use real offline artifacts"}}
    monitor, celery, consumer, remove_listeners = None, None, None, None
    users = {}
    previous_celery = current_app._get_current_object()
    prefix, queue_name = f"b14:{run_id}:", "b14-" + run_id
    _truncate(postgresql_engine)
    try:
        password = "B14 isolated synthetic load password"
        users, index_numbers, control_numbers = seed_load_data(postgresql_engine, password, run_id)
        monkeypatch.setenv("REDIS_URL", redis_url)
        monkeypatch.setenv("PYCODE_JWT_SECRET", secrets.token_urlsafe(48))
        monitor = Redis.from_url(redis_url, socket_connect_timeout=1, socket_timeout=1)
        monitor.ping()
        redis_server = monitor.info("server")
        report["redis"] = {key: redis_server.get(key) for key in ("redis_version", "os", "arch_bits")}
        redis_endpoint = monitor.connection_pool.connection_kwargs
        report["container_resources"] = container_resources(redis_endpoint.get("host", "localhost"), redis_endpoint.get("port", 6379))
        with postgresql_engine.connect() as connection:
            report["postgresql"] = environment(connection)
        celery = create_celery_app(redis_url)
        celery.conf.broker_transport_options = {**celery.conf.broker_transport_options, "global_keyprefix": prefix}
        original_publish = celery.send_task
        publications = []

        def publish(name, *, args, queue):
            assert queue == "pycode"
            result = original_publish(name, args=args, queue=queue_name, retry=False)
            publications.append(args[0])
            return result

        monkeypatch.setattr(celery, "send_task", publish)
        metrics_token = secrets.token_urlsafe(48)
        app = create_app(database_url=database_url, adapter=application.state.adapter, workspace=application.state.workspace,
                         task_dispatcher=CeleryTaskDispatcher(lambda: celery), observability_settings=ObservabilitySettings(metrics_token))
        app.add_middleware(OperationMiddleware)
        report["admission"] = vars(app.state.admission.settings)

        from backend.app.tasks import indexing

        @contextmanager
        def worker_services():
            with app.state.database.open_session() as session:
                uow = SqlAlchemyUnitOfWork(session)
                settings = ExecutionLeaseSettings.from_environment()
                yield BackgroundTaskService(uow, lease_settings=settings,
                    heartbeat_factory=lambda task: ExecutionOwnership(app.state.database, task, settings)), build_project_service(
                        uow, adapter=app.state.adapter, workspace=app.state.workspace, artifacts=app.state.artifacts,
                        operations=app.state.project_operations)

        monkeypatch.setattr(indexing, "worker_repository_index_services", worker_services)
        with loopback_server(app) as base_url, httpx.Client(base_url=base_url, timeout=30, trust_env=False) as client, ExitStack() as cleanup:
            actors = []
            owned = defaultdict(set)
            with postgresql_engine.connect() as connection:
                for project, owner in connection.execute(text("SELECT id,owner_id FROM projects")):
                    owned[owner].add(project)
                for index, number in enumerate(index_numbers):
                    account = f"benchmark-owner-{index + 2}"
                    response = client.post("/api/v1/auth/login", json={"account_name": account, "password": password})
                    assert response.status_code == 200
                    project = identifier("project", index + 1)
                    expected = connection.execute(text("""
                        SELECT id FROM agent_runs WHERE project_id=:project AND status='completed' AND result IS NOT NULL
                        AND run_type IN ('ask','impact','agent') ORDER BY created_at DESC,id DESC LIMIT 20 OFFSET 1000
                    """), {"project": project}).scalars().all()
                    actors.append(Actor(account, {"Authorization": "Bearer " + response.json()["access_token"]},
                                        project, identifier("project", number), owned[users[index + 2]], list(map(str, expected))))
            assert client.get("/api/v1/projects").status_code == 401
            assert client.get(f"/api/v1/projects/{actors[0].history_project}/analysis-history", headers=actors[1].headers).status_code == 404
            assert client.post(f"/api/v1/projects/{actors[0].index_project}/index", headers=actors[1].headers).status_code == 404

            # Observe the real active-task/rate policy with an intentionally undrained queue.
            response = client.post("/api/v1/auth/login", json={"account_name": "benchmark-owner-50", "password": password})
            assert response.status_code == 200
            control_headers = {"Authorization": "Bearer " + response.json()["access_token"]}
            capacity = min(report["admission"]["active_tasks"], report["admission"]["index_requests"])
            assert capacity + 2 <= len(control_numbers), "Configured quota exceeds this bounded control scenario"
            control_tasks, statuses = [], []
            for index, number in enumerate(control_numbers[:capacity + 2]):
                response = client.post(f"/api/v1/projects/{identifier('project', number)}/index",
                    headers={**control_headers, "Idempotency-Key": run_id + f"-control-{index}"})
                statuses.append(response.status_code)
                if response.status_code == 202:
                    control_tasks.append(UUID(response.json()["task_id"]))
            assert statuses == [202] * capacity + [429, 429]
            count = len(publications)
            replay = client.post(f"/api/v1/projects/{identifier('project', control_numbers[0])}/index",
                headers={**control_headers, "Idempotency-Key": run_id + "-control-0"})
            assert replay.status_code == 202 and replay.json()["task_id"] == str(control_tasks[0])
            assert len(publications) == count
            report["controls"] = {"unauthenticated": 401, "foreign_history": 404, "foreign_submit": 404,
                                  "backlog_statuses": statuses, "backlog_replay": 202, "duplicate_publications": 0}
            consumer = IndexConsumer(celery, queue_name, app)
            cleanup.callback(consumer.close)
            consumer.thread.start()
            consumer.wait(control_tasks)
            workload = Workload(client, actors, password, consumer, Telemetry(), postgresql_engine, metrics_token, run_id)
            primed = [workload.request("submit", actor, run_id + f"-prime-{index}")[1] for index, actor in enumerate(actors)]
            assert all(primed)
            consumer.wait(primed)
            assert client.get("/api/v1/tasks/" + actors[0].task_id, headers=actors[1].headers).status_code == 404
            # The application owns a normal pooled Engine, not the fixture's NullPool.
            engine = app.state.database._engine
            report["pool"] = {"size": engine.pool.size(), "max_overflow": engine.pool._max_overflow,
                              "timeout_seconds": engine.pool.timeout(), "http_worker_processes": 1, "index_consumers": 1}
            remove_listeners = workload.telemetry.install(engine, monkeypatch)
            candidate = ProjectService.list_owned_page
            phase_number = 0
            workload.phase("warmup", max(levels), 20, phase_number)
            phase_number += 1
            for repeat in range(options["rounds"]):
                for concurrency in levels:
                    variants = (["before", "after"] if repeat % 2 == 0 else ["after", "before"]) if options["compare"] else ["baseline"]
                    for variant in variants:
                        monkeypatch.setattr(ProjectService, "list_owned_page", frozen_list_owned_page if variant == "before" else candidate)
                        before_db, before_redis = database_stats(postgresql_engine), monitor.info("stats")
                        phase = workload.phase(variant, concurrency, options["requests"], phase_number)
                        phase["repeat"] = repeat
                        phase["postgresql_delta"] = {key: value - before_db[key] for key, value in database_stats(postgresql_engine).items()}
                        after_redis = monitor.info("stats")
                        phase["redis_global_delta"] = {key: after_redis[key] - before_redis[key] for key in
                                                       ("total_commands_processed", "keyspace_hits", "keyspace_misses", "rejected_connections")}
                        report["phases"].append(phase)
                        print("B14", variant, concurrency, repeat, round(phase["requests_per_second"], 2), phase["operations"]["all"])
                        phase_number += 1
                        assert not any(row["status"] == 0 or row["status"] >= 500 for row in phase["samples"])
            assert llm.prompts == []
            report["llm_calls"] = 0
            assert not consumer.errors
            consumer.close()
            consumer = None
            if remove_listeners:
                remove_listeners()
                remove_listeners = None
            with postgresql_engine.connect() as connection:
                report["completed_index_tasks"] = connection.execute(text(
                    "SELECT count(*) FROM background_tasks WHERE task_type='repository_index' AND status='completed'")).scalar_one()
            assert report["completed_index_tasks"] == len(publications)
            report["comparisons"] = comparisons(report["phases"])
    except BaseException as exc:
        report["error_type"] = type(exc).__name__
        raise
    finally:
        if consumer is not None:
            consumer.close()
        if remove_listeners:
            remove_listeners()
        if celery is not None:
            celery.close()
            previous_celery.set_current()
        if monitor is not None:
            private_keys = list(monitor.scan_iter(match=prefix + "*"))
            rate_keys = [f"pycode:rate:v1:{owner}:index" for owner in users.values()]
            if private_keys or rate_keys:
                monitor.delete(*(private_keys + rate_keys))
            monitor.close()
        _truncate(postgresql_engine)
        OUTPUT.mkdir(parents=True, exist_ok=True)
        path = OUTPUT / ("B14_load_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        print("B14 measurements:", path)
