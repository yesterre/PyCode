"""Rejected B10 candidate: reproducible measurements, no production cache wiring.

Run with pytest -s. Timings are evidence, not a flaky pass/fail threshold.
The count version assumes the existing append-only completed-history state machine.
"""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from time import perf_counter_ns
from uuid import UUID, uuid4

import pytest
from pydantic import TypeAdapter
from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from backend.app.core.models import AnalysisHistoryItem
from backend.app.db.models import AgentRunORM
from backend.app.repositories import SqlAlchemyUnitOfWork
from backend.app.runtime import build_project_service
from backend.app.services.projects import _HISTORY_MODES
from b09_query_benchmark import PROJECTS, RUNS, environment, seed
from test_postgresql import _safe_test_database_url, _truncate, postgresql_engine


@pytest.fixture
def cache_redis():
    url = os.environ.get("TEST_REDIS_URL") or os.environ.get("REDIS_URL")
    if not url:
        pytest.skip("TEST_REDIS_URL or REDIS_URL is required for the cache experiment")
    client = Redis.from_url(url, socket_connect_timeout=0.1, socket_timeout=0.1,
                            retry=Retry(NoBackoff(), 0), max_connections=8)
    keys = set()
    try:
        client.ping()
        yield client, keys
    finally:
        try:
            if keys:
                client.delete(*keys)
        finally:
            client.close()


class HistoryCacheCandidate:
    """Test-only copy of the measured proposal; never imported by the application."""

    ttl_seconds = 30
    page_type = TypeAdapter(tuple[list[AnalysisHistoryItem], bool])

    def __init__(self, service, client, keys):
        self.service, self.client, self.keys = service, client, keys
        self.reads = self.writes = 0
        self.last_key = None
        self.namespace = f"pycode:b10-probe:{uuid4().hex}:history:v1"

    @staticmethod
    def version_query(project):
        return select(func.count()).select_from(AgentRunORM).where(
            AgentRunORM.project_id == project, AgentRunORM.status == "completed",
            AgentRunORM.result.is_not(None), AgentRunORM.run_type.in_(("ask", "impact", "agent")),
        )

    def read(self, project_id, limit, offset, owner_id):
        uow = self.service.uow
        with uow.transaction():
            project = uow.projects.get_owned(project_id, owner_id)
            version = uow.session.scalar(self.version_query(project_id))
            stamp = f"{project.updated_at.isoformat()}:{project.archived_at}:{version}"
            digest = hashlib.sha256(stamp.encode()).hexdigest()
            key = f"{self.namespace}:{owner_id}:{project_id}:{digest}:{limit}:{offset}"
            self.keys.add(key)
            self.last_key = key
            self.reads += 1
            try:
                raw = self.client.get(key)
                if raw is not None and len(raw) <= 1024 * 1024:
                    cached = self.page_type.validate_json(raw, strict=True)
                    if len(cached[0]) <= limit:
                        return cached
            except (RedisError, OSError, ValueError):
                pass
            runs = uow.agent_runs.list_history_for_project(project_id, limit + 1, offset)
        result = [AnalysisHistoryItem(run.id, run.created_at, _HISTORY_MODES[run.run_type],
                                     run.task, run.result) for run in runs[:limit]], len(runs) > limit
        raw = self.page_type.dump_json(result)
        if len(raw) <= 1024 * 1024:
            try:
                self.client.set(key, raw, ex=self.ttl_seconds)
                self.writes += 1
            except (RedisError, OSError):
                pass
        return result


def test_deep_history_service_cache_measurement(postgresql_engine, application, cache_redis):
    _safe_test_database_url()
    engine = postgresql_engine
    _truncate(engine)
    client, keys = cache_redis
    report = {"projects": PROJECTS, "runs": RUNS, "samples": 21, "warmups": 2,
              "scope": "Service including ownership/version SQL, Redis and hydration; warm local connections",
              "cases": {}}
    project = UUID(hashlib.md5(b"project-1").hexdigest())
    owner = UUID(hashlib.md5(b"owner-2").hexdigest())
    try:
        with engine.begin() as connection:
            seed(connection)
        with engine.connect() as connection, Session(bind=connection, expire_on_commit=False) as session:
            report["environment"] = environment(connection)
            report["redis_version"] = client.info("server")["redis_version"]
            ping_times = []
            for _ in range(21):
                start = perf_counter_ns()
                client.ping()
                ping_times.append((perf_counter_ns() - start) / 1_000_000)
            report["redis_ping_ms"] = ping_times
            report["median_redis_ping_ms"] = median(ping_times)
            report["visible_history"] = connection.execute(text("""
                SELECT count(*) FROM agent_runs WHERE project_id=:project AND status='completed'
                AND result IS NOT NULL AND run_type IN ('ask','impact','agent')
            """), {"project": project}).scalar_one()
            connection.commit()
            service = build_project_service(
                SqlAlchemyUnitOfWork(session), adapter=application.state.adapter,
                workspace=application.state.workspace, artifacts=application.state.artifacts,
                operations=application.state.project_operations,
            )
            version_sql = str(HistoryCacheCandidate.version_query(project).compile(
                dialect=connection.dialect, compile_kwargs={"literal_binds": True},
            ))
            report["version_sql"] = version_sql
            report["version_plan"] = connection.execute(text(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + version_sql,
            )).scalar_one()[0]
            connection.commit()
            cache = HistoryCacheCandidate(service, client, keys)
            for limit, offset in [(20, 1000), (100, 1000), (20, 9000)]:
                samples = {"source": [], "miss": [], "hit": []}

                def read(phase):
                    if phase == "miss":
                        # Delete only the key last read for this generated test project.
                        if cache.last_key is not None:
                            client.delete(cache.last_key)
                    start = perf_counter_ns()
                    result = (service.get_analysis_history(project, limit, offset, owner_id=owner)
                              if phase == "source" else cache.read(project, limit, offset, owner))
                    elapsed = (perf_counter_ns() - start) / 1_000_000
                    return result, elapsed

                expected, _ = read("source")
                reads_before, writes_before = cache.reads, cache.writes
                for iteration in range(23):
                    phases = ["source", "miss", "hit"] if iteration % 2 == 0 else ["miss", "hit", "source"]
                    for phase in phases:
                        result, elapsed = read(phase)
                        assert result == expected
                        if iteration >= 2:
                            samples[phase].append(elapsed)
                reads = cache.reads - reads_before
                writes = cache.writes - writes_before
                assert reads == 46 and writes == 23
                assert 0 < client.ttl(cache.last_key) <= 30
                values = {"samples_ms": samples, "median_ms": {phase: median(times) for phase, times in samples.items()},
                          "hits": reads - writes, "misses": writes, "ttl": cache.ttl_seconds}
                report["cases"][f"limit_{limit}_offset_{offset}"] = values
                print(limit, offset, values["median_ms"])
    finally:
        _truncate(engine)
        directory = Path(__file__).resolve().parents[2] / "docs_v3.0" / "v3_backend"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = directory / f"B10_cache_benchmark_{stamp}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"B10 measurements: {path}")
