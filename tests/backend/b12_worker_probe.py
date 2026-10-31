"""Dedicated process killed by B12 tests at committed Worker checkpoints."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from redis import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

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
from conftest import CopySource, FakeLLM
from test_postgresql import _safe_test_database_url


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", type=UUID, required=True)
    parser.add_argument("--checkpoint", choices=["claim", "pre_core", "post_core"], required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--signal", type=Path, required=True)
    parser.add_argument("--effect", type=Path, required=True)
    args = parser.parse_args()
    url = _safe_test_database_url()

    def checkpoint(stage):
        args.signal.write_text(json.dumps({"stage": stage, "pid": os.getpid(), "task": str(args.task)}),
                               encoding="utf-8")
        while True:
            time.sleep(0.1)  # The parent kills only this dedicated child process.

    database = DatabaseSessionManager(url)
    session = database.open_session()
    redis = Redis.from_url(os.environ["B12_TEST_REDIS_URL"], socket_connect_timeout=1,
                           socket_timeout=1, retry=Retry(NoBackoff(), 0))
    try:
        uow = SqlAlchemyUnitOfWork(session)
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
        if args.checkpoint == "claim":
            # execute() has committed the claim before entering this method.
            service._execute_claimed = lambda *a, **kw: checkpoint("claim")
        elif args.checkpoint == "pre_core":
            original = projects.execute_on_snapshot

            def before_core(project, snapshot, operation):
                return original(project, snapshot, lambda *a: checkpoint("pre_core"))

            projects.execute_on_snapshot = before_core
        else:
            def after_core(repository, task_text):
                # A durable, isolated stand-in for a non-repeatable external effect.
                with args.effect.open("a", encoding="utf-8") as output:
                    output.write("entered-core\n")
                    output.flush()
                    os.fsync(output.fileno())
                checkpoint("post_core")

            adapter.run_agent = after_core
        service.execute(args.task, worker_id=f"b12-probe:{os.getpid()}")
        raise AssertionError("Worker must reach the requested checkpoint")
    finally:
        session.close()
        database.dispose()
        redis.close()


if __name__ == "__main__":
    main()
