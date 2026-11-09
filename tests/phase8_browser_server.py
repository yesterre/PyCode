"""Controlled Phase 8 browser server: real FastAPI routes, no paid model or external services."""

import asyncio
import json
import shutil
import tempfile
import threading
import time
import os
from pathlib import Path
from uuid import UUID

# Set isolation before importing the default backend application, which loads configuration.
os.environ['PYTHON_DOTENV_DISABLED'] = '1'
os.environ['PYCODE_EMBEDDING_ENABLED'] = 'false'
os.environ['PYCODE_RETRIEVAL_MODE'] = 'keyword'

from backend.app.core.application_trace import ApplicationEventType, ApplicationTraceEvent
from backend.app.config import AuthSettings
from backend.app.auth import hash_password
from backend.app.infrastructure.realtime_trace import event_json, stream_key
from backend.app.infrastructure.repositories import GitRepositorySource, RepositoryRevision, RepositoryWorkspace
from backend.app.integrations.pycode import PyCodeAdapter
from backend.app.main import create_app
from backend.app.repositories import InMemoryPersistence, InMemoryUnitOfWork
from backend.app.runtime import build_project_service, build_agent_run_service
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


class ControlledLLM:
    def generate(self, prompt: str) -> str:
        time.sleep(0.3)  # Keep live SSE observable without a remote model.
        if prompt.startswith('You are the PyCode LLM Planner.'):
            payload = json.loads(prompt.split('Planning input:\n')[-1])
            if payload.get('initial_evidence'):
                question = payload['initial_evidence']['question']
                return json.dumps([dict(tool='retrieve_context',arguments={'question':question},reason='inspect seed',required=False)])
        if prompt.startswith('You are the PyCode LLM Next-Action Planner.'):
            payload = json.loads(prompt.split('Next-action input:\n')[-1])
            if any(section['name']=='initial_evidence' for section in payload['context_sections']) or any(
                    result.get('data',{}).get('initial_evidence_reused') for result in payload['recent_tool_results']):
                if not payload['recent_tool_results']:
                    section = next(s for s in payload['context_sections'] if s['name']=='initial_evidence')
                    question = json.loads(section['content'])['question']
                    return json.dumps(dict(action_type='tool_call',tool_name='retrieve_context',arguments={'question':question},reason='inspect seed'))
                return json.dumps(dict(action_type='final_answer',final_answer='Controlled upgraded answer.',reason='limited evidence'))
        return "Controlled repository answer."


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
        with uow.transaction():
            project_id = uow.background_tasks.get(task_id).project_id
        projects = build_project_service(
            uow, adapter=app.state.adapter, workspace=app.state.workspace,
            operations=app.state.project_operations, artifacts=app.state.artifacts,
        )
        tasks.execute_repository_index(task_id, lambda project_id: (
            time.sleep(1.5), projects.index(project_id))[1])
        with uow.transaction():
            repository = uow.agent_runs.create(
                project_id, None, task="Where is the entry point?", run_type="ask", model=None,
            )
            uow.agent_runs.complete(repository.id, "The entry point is main.py.")
            impact = uow.agent_runs.create(
                project_id, None, task="service.py", run_type="impact", model=None,
            )
            uow.agent_runs.complete(impact.id, "Changes affect main.py.")

    def _agent(self, task_id):
        time.sleep(0.7)
        app = self.app
        uow = InMemoryUnitOfWork(app.state.persistence)
        projects = build_project_service(uow, adapter=app.state.adapter, workspace=app.state.workspace,
            artifacts=app.state.artifacts, operations=app.state.project_operations)
        from types import SimpleNamespace
        service = build_agent_run_service(uow, background_tasks=BackgroundTaskService(uow),
            projects=projects, adapter=app.state.adapter, agent_workspace=app.state.agent_workspace,
            trace_publisher=SimpleNamespace(publish=app.state.realtime_redis.append))
        service.execute(task_id)


_temporary_root = Path(".tmp").resolve()
_temporary_root.mkdir(parents=True, exist_ok=True)
_root = Path(tempfile.mkdtemp(prefix="phase8-browser-", dir=_temporary_root))
_source = _root / "source"
_source.mkdir()
(_source / "main.py").write_text("from service import answer\ndef main():\n    return answer()\n", encoding="utf-8")
(_source / "service.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
_dispatcher = Dispatcher()
app = create_app(adapter=PyCodeAdapter(llm_client=ControlledLLM()),
    workspace=RepositoryWorkspace(_root / "workspaces", CopySource(_source)),
    persistence=InMemoryPersistence(), task_dispatcher=_dispatcher,
    auth_settings=AuthSettings('browser-test-only-' + 'x'*48))
_users = InMemoryUnitOfWork(app.state.persistence)
with _users.transaction():
    _users.users.create('browser-user', hash_password('browser-test-password'))
app.state.realtime_redis = FakeRedis()
_dispatcher.app = app
