from pathlib import Path
import shutil
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from backend.app.integrations.pycode import PyCodeAdapter
from backend.app.main import create_app
from backend.app.infrastructure.repositories import (
    GitRepositorySource, RepositoryRevision, RepositoryWorkspace,
)
from backend.app.repositories import InMemoryPersistence
from backend.app.repositories import InMemoryUnitOfWork
from backend.app.runtime import build_project_service
from backend.app.services.background_tasks import BackgroundTaskService


REPO_URL = "https://github.com/example/demo.git"


class CopySource(GitRepositorySource):
    """Offline source double for existing analysis/error tests; Git has separate tests."""

    def __init__(self, root):
        super().__init__()
        self.repositories = {REPO_URL: root}
        self.calls = []

    def clone(self, repo_url, branch, destination, *, project_id=None):
        self.calls.append((repo_url, branch, destination))
        shutil.copytree(self.repositories[repo_url], destination, symlinks=True)
        (destination / ".git").mkdir(exist_ok=True)
        return RepositoryRevision(branch or "default", "a" * 40)

    def update(self, repo_url, branch, repository, *, project_id=None):
        return RepositoryRevision(branch or "default", "a" * 40)

    def checkout_exact(self, repository, commit_sha, *, project_id=None):
        if commit_sha != "a" * 40:
            raise AssertionError("unexpected fake Commit")

    def protect_snapshot(
        self, repository, snapshot_id, commit_sha, *, project_id=None,
    ):
        if commit_sha != "a" * 40:
            raise AssertionError("unexpected fake Commit")

    def commit_available(self, repository, commit_sha, *, project_id=None):
        return commit_sha == "a" * 40

    def resolve_head(self, repository, *, project_id=None):
        return "a" * 40


class FakeLLM:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return "Offline HTTP answer"


class RecordingTaskDispatcher:
    def __init__(self) -> None:
        self.health_task_ids: list[UUID] = []
        self.repository_index_task_ids: list[UUID] = []

    def enqueue_health(self, task_id: UUID) -> None:
        self.health_task_ids.append(task_id)

    def enqueue_repository_index(self, task_id: UUID) -> None:
        self.repository_index_task_ids.append(task_id)


class InlineIndexWorker:
    """Run the production application services without requiring Redis in tests."""

    def __init__(self, application) -> None:
        self.application = application

    def submit(self, client: TestClient, project_url: str):
        response = client.post(project_url + "/index")
        assert response.status_code == 202, response.text
        return response

    def execute(self, task_id: UUID):
        unit_of_work = InMemoryUnitOfWork(self.application.state.persistence)
        background_tasks = BackgroundTaskService(unit_of_work)
        projects = build_project_service(
            unit_of_work,
            adapter=self.application.state.adapter,
            workspace=self.application.state.workspace,
            artifacts=self.application.state.artifacts,
            operations=self.application.state.project_operations,
        )
        return background_tasks.execute_repository_index(task_id, projects.index)

    def run(self, client: TestClient, project_url: str):
        accepted = self.submit(client, project_url)
        self.execute(UUID(accepted.json()["task_id"]))
        return client.get(project_url)


@pytest.fixture
def source_repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    (root / "service.py").write_text("def serve():\n    return 42\n", encoding="utf-8")
    (root / "main.py").write_text(
        "from service import serve\ndef main():\n    return serve()\n"
        "if __name__ == '__main__':\n    main()\n", encoding="utf-8",
    )
    return root


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def adapter(llm) -> PyCodeAdapter:
    return PyCodeAdapter(llm_client=llm)


@pytest.fixture
def persistence():
    return InMemoryPersistence()


@pytest.fixture
def application(adapter, tmp_path, source_repository, persistence):
    workspace = RepositoryWorkspace(tmp_path / "workspaces", CopySource(source_repository))
    return create_app(
        adapter=adapter, workspace=workspace, persistence=persistence,
        task_dispatcher=RecordingTaskDispatcher(),
    )


@pytest.fixture
def client(application):
    with TestClient(application) as client:
        yield client


@pytest.fixture
def index_worker(application):
    return InlineIndexWorker(application)


@pytest.fixture
def index_worker_factory():
    return InlineIndexWorker


@pytest.fixture
def task_dispatcher_factory():
    return RecordingTaskDispatcher


@pytest.fixture
def project_url(client):
    response = client.post("/api/v1/projects", json={"name": "Example", "repo_url": REPO_URL})
    assert response.status_code == 201, response.text
    return "/api/v1/projects/" + response.json()["id"]


@pytest.fixture
def repository(application, project_url):
    return application.state.workspace.path_for(UUID(project_url.rsplit("/", 1)[-1]))


@pytest.fixture
def ready_url(client, project_url, index_worker):
    response = index_worker.run(client, project_url)
    assert response.status_code == 200, response.text
    return project_url
