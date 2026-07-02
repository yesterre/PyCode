from pathlib import Path
import shutil
import subprocess
import uuid

from pycode.tools import ToolContext, run_pytest


def test_run_pytest_requires_explicit_permission() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        project_path.mkdir()

        result = run_pytest(ToolContext(project_path), ["tests"])

        assert result.ok is False
        assert result.summary == "Test execution is not allowed."
    finally:
        _cleanup(workspace)


def test_run_pytest_executes_controlled_pytest_command(monkeypatch) -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        tests_dir = project_path / "tests"
        tests_dir.mkdir(parents=True)

        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(
                args[0],
                0,
                stdout="1 passed",
                stderr="",
            )

        monkeypatch.setattr(subprocess, "run", fake_run)

        result = run_pytest(
            ToolContext(project_path, allow_tests=True),
            ["tests"],
            extra_args=["--basetemp=.pytest_tmp", "--cache-clear"],
        )

        assert result.ok is True
        assert result.data["exit_code"] == 0
        assert result.data["command"][1:3] == ["-m", "pytest"]
        assert result.data["stdout"] == "1 passed"
    finally:
        _cleanup(workspace)


def test_run_pytest_blocks_test_path_outside_project() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        project_path.mkdir()

        result = run_pytest(
            ToolContext(project_path, allow_tests=True),
            [workspace / "outside_tests"],
        )

        assert result.ok is False
        assert result.summary == "Test path denied."
    finally:
        _cleanup(workspace)


def _workspace() -> Path:
    path = Path(".pytest_tmp_tools") / f"test_runner_{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    return path.resolve()


def _cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
