from pathlib import Path
import shutil
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from backend.app.integrations.pycode import PyCodeAdapter
from backend.app.main import create_app
from backend.app.infrastructure.repositories import GitRepositorySource, RepositoryWorkspace
from backend.app.repositories import InMemoryPersistence


REPO_URL = "https://github.com/example/demo.git"


class CopySource(GitRepositorySource):
    """Offline source double for existing analysis/error tests; Git has separate tests."""

    def __init__(self, root):
        super().__init__()
        self.repositories = {REPO_URL: root}
        self.calls = []

    def clone(self, repo_url, branch, destination):
        self.calls.append((repo_url, branch, destination))
        shutil.copytree(self.repositories[repo_url], destination, symlinks=True)
        (destination / ".git").mkdir(exist_ok=True)

    def resolve_head(self, repository):
        return "a" * 40


class FakeLLM:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return "Offline HTTP answer"


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
    return create_app(adapter=adapter, workspace=workspace, persistence=persistence)


@pytest.fixture
def client(application):
    with TestClient(application) as client:
        yield client


@pytest.fixture
def project_url(client):
    response = client.post("/api/v1/projects", json={"name": "Example", "repo_url": REPO_URL})
    assert response.status_code == 201, response.text
    return "/api/v1/projects/" + response.json()["id"]


@pytest.fixture
def repository(application, project_url):
    return application.state.workspace.path_for(UUID(project_url.rsplit("/", 1)[-1]))


@pytest.fixture
def ready_url(client, project_url):
    response = client.post(project_url + "/index")
    assert response.status_code == 200, response.text
    return project_url
