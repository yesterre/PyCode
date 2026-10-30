"""B09 reproducible measurements using the real repositories and guarded DB fixture."""
import json
import os
import platform
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from uuid import UUID

from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from backend.app.repositories import SqlAlchemyUnitOfWork
from backend.app.db.models import AgentRunORM


PROJECTS = 80_000
RUNS = 160_000
REPEATS = 9


def seed(connection):
    connection.execute(text("""
        INSERT INTO users (id,account_name,password_hash)
        SELECT md5('owner-'||i)::uuid, 'benchmark-owner-'||i, 'benchmark-hash'
        FROM generate_series(1,50) i
    """))
    connection.execute(text("""
        INSERT INTO projects (id,name,repo_url,owner_id,status,created_at,updated_at,archived_at)
        SELECT md5('project-'||i)::uuid, 'benchmark-project-'||i,
               'https://github.com/example/benchmark.git', md5('owner-'||(i%50+1))::uuid,
               'created', '2026-09-01'::timestamptz,
               '2026-09-01'::timestamptz + (i/50%30)*interval '1 second',
               CASE WHEN i/50%5=0 THEN '2026-10-01'::timestamptz ELSE NULL END
        FROM generate_series(1,:count) i
    """), {"count": PROJECTS})
    connection.execute(text("""
        INSERT INTO agent_runs (id,project_id,task,run_type,status,result,created_at)
        SELECT md5('run-'||i)::uuid, md5('project-'||(i%20+1))::uuid,
               repeat('question ',32),
               (ARRAY['ask','impact','agent','unknown','ask'])[i/20%5+1],
               CASE i/20%10 WHEN 0 THEN 'queued' WHEN 1 THEN 'running'
                    WHEN 2 THEN 'failed' ELSE 'completed' END,
               CASE WHEN i/20%11=0 OR i/20%10<3 THEN NULL ELSE repeat('answer ',128) END,
               '2026-09-01'::timestamptz + (i/20%30)*interval '1 second'
        FROM generate_series(1,:count) i
    """), {"count": RUNS})
    connection.execute(text("ANALYZE projects"))
    connection.execute(text("ANALYZE agent_runs"))


def repository_sql(connection, operation):
    statements = []
    with Session(bind=connection) as session:
        event.listen(session, "do_orm_execute", lambda state: statements.append(state.statement))
        result = operation(SqlAlchemyUnitOfWork(session))
    assert len(statements) == 1
    return str(statements[0].compile(dialect=connection.dialect, compile_kwargs={"literal_binds": True})), result


def measure(connection, sql, *, write=False):
    samples, plan = [], None
    for iteration in range(REPEATS + 2):
        transaction = connection.begin_nested() if write else None
        try:
            plan = connection.execute(text("EXPLAIN (ANALYZE, BUFFERS, WAL, FORMAT JSON) " + sql)).scalar_one()[0]
        finally:
            if transaction is not None:
                transaction.rollback()
        if iteration >= 2:
            samples.append({"planning_ms": plan["Planning Time"], "execution_ms": plan["Execution Time"]})
    return {"sql": sql, "samples": samples,
            "median_execution_ms": median(sample["execution_ms"] for sample in samples), "plan": plan}


def environment(connection):
    names = ["server_version", "shared_buffers", "work_mem", "random_page_cost", "effective_cache_size",
             "jit", "max_parallel_workers_per_gather", "synchronous_commit"]
    return {**{name: connection.execute(text("SHOW " + name)).scalar_one() for name in names},
            "server": connection.execute(text("SELECT version()")).scalar_one(),
            "database": connection.execute(text("SELECT current_database()")).scalar_one(),
            "alembic_revision": connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one(),
            "client_platform": platform.platform(), "python": platform.python_version(),
            "client_logical_cpus": os.cpu_count()}


def save_report(name, report):
    directory = Path(__file__).resolve().parents[2] / "docs_v3.0" / "v3_backend"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = directory / f"B09_{name}_{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def history_cases(connection):
    import hashlib

    project = UUID(hashlib.md5(b"project-1").hexdigest())
    cases = {}
    for page_limit in [1, 20, 100]:
        for offset in [0, 1000, 9000]:
            # Frozen query from pre-B09 HEAD a28d5e2; no production fallback.
            before = (select(AgentRunORM).where(
                AgentRunORM.project_id == project, AgentRunORM.status == "completed",
                AgentRunORM.result.is_not(None), AgentRunORM.run_type.in_(("ask", "impact", "agent")),
            ).order_by(AgentRunORM.created_at.desc(), AgentRunORM.id.desc())
                .limit(page_limit + 1).offset(offset))
            after, _ = repository_sql(connection, lambda uow: uow.agent_runs.list_history_for_project(
                project, page_limit + 1, offset,
            ))
            baseline_sql = str(before.compile(dialect=connection.dialect, compile_kwargs={"literal_binds": True}))
            cases[f"limit_{page_limit}_offset_{offset}"] = {"before": baseline_sql, "after": after}
    return cases


def indexes(connection):
    return [dict(row) for row in connection.execute(text("""
        SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname=current_schema()
        AND tablename IN ('projects','agent_runs') ORDER BY tablename,indexname
    """)).mappings()]


def write_controls(connection, sql):
    """Alternating identical writes: read-only optimization must not change writes."""
    samples, plans = {"before": [], "after": []}, {}
    for iteration in range(REPEATS + 2):
        for phase in (["before", "after"] if iteration % 2 == 0 else ["after", "before"]):
            transaction = connection.begin_nested()
            try:
                plan = connection.execute(text("EXPLAIN (ANALYZE, BUFFERS, WAL, FORMAT JSON) " + sql)).scalar_one()[0]
            finally:
                transaction.rollback()
            plans[phase] = plan
            if iteration >= 2:
                samples[phase].append({"planning_ms": plan["Planning Time"], "execution_ms": plan["Execution Time"]})
    return {phase: {"sql": sql, "samples": values,
                    "median_execution_ms": median(v["execution_ms"] for v in values), "plan": plans[phase]}
            for phase, values in samples.items()}


PROJECT_INSERT = """INSERT INTO projects (id,name,repo_url,owner_id,status)
    SELECT md5('write-project-'||i)::uuid, 'write', 'https://github.com/example/demo.git',
           md5('owner-1')::uuid, 'created' FROM generate_series(1,500) i"""
PROJECT_UPDATE = """UPDATE projects SET status='preparing',updated_at=now()
    WHERE id IN (SELECT id FROM projects WHERE archived_at IS NULL ORDER BY id LIMIT 500)"""
RUN_INSERT = """INSERT INTO agent_runs (id,project_id,task,run_type,status)
    SELECT md5('write-run-'||i)::uuid, md5('project-1')::uuid, 'write', 'agent', 'queued'
    FROM generate_series(1,500) i"""
RUN_COMPLETE = """UPDATE agent_runs SET status='completed',result='answer',finished_at=now()
    WHERE id IN (SELECT id FROM agent_runs WHERE status='running' ORDER BY id LIMIT 500)"""
