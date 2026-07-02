import subprocess
from pathlib import Path
import shutil
import uuid

from pycode.tools import ToolContext, get_changed_files, get_git_diff


def test_get_git_diff_collects_diff_output(
    monkeypatch,
) -> None:
    workspace = _workspace()
    project_path = workspace / "project"
    project_path.mkdir()

    try:
        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], 0, stdout="diff output", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)

        result = get_git_diff(ToolContext(project_path))

        assert result.ok is True
        assert result.data["command"] == ["git", "diff"]
        assert result.data["diff"] == "diff output"
    finally:
        _cleanup(workspace)


def test_get_changed_files_splits_git_output(monkeypatch) -> None:
    workspace = _workspace()
    project_path = workspace / "project"
    project_path.mkdir()

    try:
        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(
                args[0],
                0,
                stdout="pycode/tools/base.py\nREADME.md\n",
                stderr="",
            )

        monkeypatch.setattr(subprocess, "run", fake_run)

        result = get_changed_files(ToolContext(project_path))

        assert result.ok is True
        assert result.data["files"] == ["pycode/tools/base.py", "README.md"]
    finally:
        _cleanup(workspace)


def test_get_git_diff_blocks_path_outside_project() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        project_path.mkdir()

        result = get_git_diff(ToolContext(project_path), path=workspace / "outside.py")

        assert result.ok is False
        assert result.summary == "Git diff path denied."
    finally:
        _cleanup(workspace)


def _workspace() -> Path:
    path = Path(".pytest_tmp_tools") / f"git_tools_{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    return path.resolve()


def _cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
