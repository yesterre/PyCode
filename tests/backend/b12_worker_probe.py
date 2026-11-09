"""Dedicated process killed by B12 tests at committed Worker checkpoints."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from b12_probe_diagnostics import StageRecorder

diagnostics = None


def main():
    global diagnostics
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", type=UUID, required=True)
    parser.add_argument("--checkpoint", choices=["claim", "pre_core", "post_core"], required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--signal", type=Path, required=True)
    parser.add_argument("--effect", type=Path, required=True)
    parser.add_argument("--diagnostics", type=Path, required=True)
    args = parser.parse_args()
    diagnostics = StageRecorder(args.diagnostics / "child-events.jsonl")
    diagnostics.record("python_started", executable=sys.executable, prefix=sys.prefix,
        base_prefix=sys.base_prefix, cwd=str(Path.cwd()), sys_path=list(sys.path),
        pythonpath_entries=os.environ.get("PYTHONPATH", "").split(os.pathsep))
    diagnostics.record("dependency_imports_started")
    diagnostics.record("redis_imports_started")
    from redis import Redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry
    diagnostics.record("redis_imports_completed")
    diagnostics.record("backend_imports_started")
    from sqlalchemy import event
    from backend.app.config import ExecutionLeaseSettings
    from backend.app.db.session import DatabaseSessionManager
    from backend.app.infrastructure.agent_workspace import AgentSnapshotWorkspace
    from backend.app.infrastructure.artifacts import SnapshotArtifactStore
    from backend.app.infrastructure.execution import ExecutionOwnership
    from backend.app.infrastructure.projects import ProjectOperationLocks, RedisProjectLockBackend
    from backend.app.infrastructure.repositories import RepositoryWorkspace
    from backend.app.integrations.pycode import PyCodeAdapter
    from backend.app.repositories import SqlAlchemyUnitOfWork
    from backend.app.runtime import build_agent_run_service, build_project_service
    from backend.app.services.background_tasks import BackgroundTaskService
    diagnostics.record("backend_imports_completed")
    diagnostics.record("fixture_imports_started")
    import conftest as fixtures
    from test_postgresql import _safe_test_database_url
    CopySource, FakeLLM = fixtures.CopySource, fixtures.FakeLLM
    diagnostics.record("fixture_imports_completed", fixture_module=fixtures.__file__)
    diagnostics.record("dependency_imports_completed")
    url = _safe_test_database_url()
    diagnostics.record("database_url_validated", database="pycode_test")

    def checkpoint(stage):
        diagnostics.record("checkpoint_write_started", checkpoint=stage, path=str(args.signal))
        written = args.signal.write_text(json.dumps({"stage": stage, "pid": os.getpid(), "task": str(args.task)}),
                               encoding="utf-8")
        diagnostics.record("checkpoint_write_completed", checkpoint=stage, characters_written=written)
        while True:
            time.sleep(0.1)  # The parent kills only this dedicated child process.

    diagnostics.record("session_factory_started")
    database = DatabaseSessionManager(url)
    session = database.open_session()
    diagnostics.record("session_factory_ready", connection_is_lazy=True)
    event.listen(session.get_bind(), "do_connect",
                 lambda *a, **kw: diagnostics.record("database_connect_started"))
    event.listen(session.get_bind(), "connect",
                 lambda *a, **kw: diagnostics.record("database_connected"))
    redis = Redis.from_url(os.environ["B12_TEST_REDIS_URL"], socket_connect_timeout=1,
                           socket_timeout=1, retry=Retry(NoBackoff(), 0))
    try:
        uow = SqlAlchemyUnitOfWork(session)
        original_claim = uow.background_tasks.claim

        def observed_claim(*a, **kw):
            diagnostics.record("claim_started")
            result = original_claim(*a, **kw)
            diagnostics.record("claim_returned", claim_status=result.status.value)
            return result

        uow.background_tasks.claim = observed_claim
        lease = ExecutionLeaseSettings(ttl_seconds=2, heartbeat_interval_seconds=0.2)
        background = BackgroundTaskService(uow, lease_settings=lease,
            heartbeat_factory=lambda task: ExecutionOwnership(database, task, lease))
        adapter = PyCodeAdapter(llm_client=FakeLLM())
        workspace = RepositoryWorkspace(args.workspace, CopySource(args.source))
        projects = build_project_service(uow, adapter=adapter, workspace=workspace,
            artifacts=SnapshotArtifactStore(args.workspace), operations=ProjectOperationLocks(
                RedisProjectLockBackend(client=redis), ttl_seconds=1, renewal_interval_seconds=0.15,
            ))
        service = build_agent_run_service(uow, background_tasks=background, projects=projects,
                                          adapter=adapter, agent_workspace=AgentSnapshotWorkspace())
        diagnostics.record("services_ready")
        original_execute_claimed = service._execute_claimed

        def after_claim(*a, **kw):
            # execute() has committed the claim before entering this method.
            diagnostics.record("claim_transaction_committed")
            if args.checkpoint == "claim":
                return checkpoint("claim")
            return original_execute_claimed(*a, **kw)

        service._execute_claimed = after_claim
        if args.checkpoint == "pre_core":
            original = projects.execute_on_snapshot

            def before_core(project, snapshot, operation):
                return original(project, snapshot, lambda *a: checkpoint("pre_core"))

            projects.execute_on_snapshot = before_core
        elif args.checkpoint == "post_core":
            def after_core(repository, task_text):
                # A durable, isolated stand-in for a non-repeatable external effect.
                with args.effect.open("a", encoding="utf-8") as output:
                    output.write("entered-core\n")
                    output.flush()
                    os.fsync(output.fileno())
                checkpoint("post_core")

            adapter.run_agent = after_core
        diagnostics.record("service_execute_started")
        service.execute(args.task, worker_id=f"b12-probe:{os.getpid()}")
        raise AssertionError("Worker must reach the requested checkpoint")
    finally:
        session.close()
        database.dispose()
        redis.close()


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        if diagnostics is not None:
            diagnostics.record("probe_exception", exception_type=type(exc).__name__)
        raise
