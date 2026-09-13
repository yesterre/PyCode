import ast
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest

from backend.app.core.errors import BackendError


def make_symlink(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        pytest.skip(f"OS does not allow symlink creation: {exc}")


def test_impact_symlink_escape_is_denied(client, ready_url, repository, tmp_path, llm):
    outside = tmp_path / "outside.py"
    outside.write_text("secret = 'outside'\n", encoding="utf-8")
    make_symlink(repository / "link.py", outside)
    response = client.post(ready_url + "/impact", json={"file_path": "link.py"})
    assert response.status_code == 403 and llm.prompts == []


def test_artifact_directory_symlink_cannot_redirect_writes(
    client, project_url, source_repository, tmp_path, index_worker,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    make_symlink(source_repository / ".pclens", outside, directory=True)
    response = index_worker.submit(client, project_url)
    with pytest.raises(BackendError) as failure:
        index_worker.execute(UUID(response.json()["task_id"]))
    assert failure.value.code == "unsafe_repository"
    assert list(outside.iterdir()) == []
    assert client.get(project_url).json()["status"] == "failed"


def test_source_symlink_cannot_redirect_scanner(
    client, project_url, source_repository, tmp_path, index_worker,
):
    outside = tmp_path / "outside.py"
    outside.write_text("private = 1\n", encoding="utf-8")
    make_symlink(source_repository / "linked.py", outside)
    response = index_worker.submit(client, project_url)
    with pytest.raises(BackendError) as failure:
        index_worker.execute(UUID(response.json()["task_id"]))
    assert failure.value.code == "unsafe_repository"


def test_backend_imports_do_not_use_cli_or_presentation():
    script = '''
import importlib.abc
import sys
class BlockPresentation(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == n or fullname.startswith(n + '.') for n in ('pycode.cli', 'pycode.rich_output', 'streamlit', 'rich', 'ui')):
            raise AssertionError('Presentation dependency: ' + fullname)
sys.meta_path.insert(0, BlockPresentation())
from backend.app.main import create_app
assert '/health' in create_app().openapi()['paths']
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_impact_rejects_resolved_escape_even_when_relative_path_looks_safe(
    client, ready_url, repository, tmp_path, llm, monkeypatch,
):
    outside = tmp_path / "external.py"
    outside.write_text("private = 1\n", encoding="utf-8")
    apparent = repository / "linked.py"
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        return outside if path == apparent else original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    response = client.post(ready_url + "/impact", json={"file_path": "linked.py"})
    assert response.status_code == 403 and llm.prompts == []


def test_artifact_resolved_escape_is_rejected_before_core_write(
    client, project_url, repository, tmp_path, monkeypatch, index_worker,
):
    apparent = repository.parent / "artifacts"
    outside = tmp_path / "external-artifacts"
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        return outside if path == apparent else original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    response = index_worker.submit(client, project_url)
    with pytest.raises(BackendError) as failure:
        index_worker.execute(UUID(response.json()["task_id"]))
    assert failure.value.code == "unsafe_repository"
    assert not outside.exists() and list(apparent.iterdir()) == []


def test_routers_and_services_obey_core_dependency_direction():
    root = Path(__file__).resolve().parents[2]
    for folder in (root / "backend/app/api", root / "backend/app/services"):
        for path in folder.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            modules = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
            modules += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
            assert not any(name == "pycode" or name.startswith("pycode.") for name in modules), path
