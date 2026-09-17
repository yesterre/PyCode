"""Controlled Phase 8 browser server: real FastAPI routes, no paid model or external services."""

import asyncio
import json
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from backend.app.core.application_trace import ApplicationEventType, ApplicationTraceEvent
from backend.app.infrastructure.realtime_trace import event_json, stream_key
from backend.app.infrastructure.repositories import GitRepositorySource, RepositoryRevision, RepositoryWorkspace
from backend.app.integrations.pycode import PyCodeAdapter
from backend.app.main import create_app
from backend.app.repositories import InMemoryPersistence, InMemoryUnitOfWork
from backend.app.runtime import build_project_service
from backend.app.services.background_tasks import BackgroundTaskService


class CopySource(GitRepositorySource):
    def __init__(self, root: Path):
        super().__init__()
        self.root = root

    def clone(self, repo_url, branch, destination, *, project_id=None):
        shutil.copytree(self.root, destination)
        (destination / ".git").mkdir(exist_ok=True)
        return RepositoryRevision(branch or "main", "a" * 40)

    def update(self, repo_url, branch, repository, *, project_id=None):
        return RepositoryRevision(branch or "main", "a" * 40)

    def checkout_exact(self, repository, commit_sha, *, project_id=None):
        return None

    def protect_snapshot(self, repository, snapshot_id, commit_sha, *, project_id=None):
        return None

    def commit_available(self, repository, commit_sha, *, project_id=None):
        return True

    def resolve_head(self, repository, *, project_id=None):
        return "a" * 40


class FakeRedis:
    def __init__(self):
        self.guard = threading.Lock()
        self.values: dict[str, list[tuple[bytes, dict[bytes, bytes]]]] = {}

    def append(self, run_id: UUID, event: ApplicationTraceEvent):
        key = stream_key(run_id)
        with self.guard:
            self.values.setdefault(key, []).append((f"{event.sequence + 1}-0".encode(),
                                                    {b"event": event_json(event).encode()}))

    async def xrange(self, key):
        with self.guard:
            return list(self.values.get(key, []))

    async def xread(self, streams, count=100, block=5000):
        key, cursor = next(iter(streams.items()))
        for _ in range(10):
            with self.guard:
                result = [item for item in self.values.get(key, [])
                          if int(item[0].split(b"-")[0]) > int(cursor.split("-")[0])]
            if result:
                return [(key, result[:count])]
            await asyncio.sleep(0.1)
        return []

    async def aclose(self):
        pass


class Dispatcher:
    def __init__(self):
        self.app = None

    def enqueue_health(self, task_id):
        pass

    def enqueue_repository_index(self, task_id):
        threading.Thread(target=self._index, args=(task_id,), daemon=True).start()

    def enqueue_agent_run(self, task_id):
        threading.Thread(target=self._agent, args=(task_id,), daemon=True).start()

    def _index(self, task_id):
        time.sleep(1.7)
        app = self.app
        uow = InMemoryUnitOfWork(app.state.persistence)
        tasks = BackgroundTaskService(uow)
        projects = build_project_service(
            uow, adapter=app.state.adapter, workspace=app.state.workspace,
            operations=app.state.project_operations, artifacts=app.state.artifacts,
        )
        tasks.execute_repository_index(task_id, lambda project_id: (
            time.sleep(1.5), projects.index(project_id))[1])

    def _agent(self, task_id):
        time.sleep(0.7)
        app = self.app
        uow = InMemoryUnitOfWork(app.state.persistence)
        with uow.transaction():
            task = uow.background_tasks.get(task_id)
            run = uow.agent_runs.mark_running(task.agent_run_id)
        specifications = [
            (ApplicationEventType.RUN_STARTED, None, {"run_type": "agent", "snapshot_id": str(run.snapshot_id)}),
            (ApplicationEventType.PLAN_CREATED, None, {"source": "rule", "fallback_used": False,
                "steps": [{"step_index": 0, "tool": "read_file", "reason": "Inspect the entry point", "required": True}]}),
            (ApplicationEventType.CONTEXT_UPDATED, None, {"included_sections": ["project", "evidence"], "turn_index": 0}),
            (ApplicationEventType.TOOL_STARTED, "read_file", {"turn_index": 0, "arguments": {"path": "main.py"}}),
            (ApplicationEventType.TOOL_FINISHED, "read_file", {"outcome": "success", "duration_ms": 50,
                "summary": "Read main.py"}),
            (ApplicationEventType.RUN_COMPLETED, None, {"stop_reason": "completed", "has_result": True,
                "trace_complete": True}),
        ]
        events = []
        for sequence, (kind, tool_name, payload) in enumerate(specifications):
            event = ApplicationTraceEvent(sequence=sequence, event_type=kind,
                occurred_at=datetime.now(timezone.utc), tool_name=tool_name, payload=payload)
            events.append(event)
            app.state.realtime_redis.append(run.id, event)
            time.sleep(0.75)
        with uow.transaction():
            for event in events:
                uow.trace_events.create(run.id, event.sequence, event.event_type.value,
                                        tool_name=event.tool_name, payload=event.database_payload())
            uow.agent_runs.complete(run.id, "Controlled analysis complete.")


_root = Path(tempfile.mkdtemp(prefix="phase8-browser-", dir=Path(".tmp").resolve()))
_source = _root / "source"
_source.mkdir()
(_source / "main.py").write_text("from service import answer\ndef main():\n    return answer()\n", encoding="utf-8")
(_source / "service.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
_dispatcher = Dispatcher()
app = create_app(adapter=PyCodeAdapter(),
    workspace=RepositoryWorkspace(_root / "workspaces", CopySource(_source)),
    persistence=InMemoryPersistence(), task_dispatcher=_dispatcher)
app.state.realtime_redis = FakeRedis()
_dispatcher.app = app
