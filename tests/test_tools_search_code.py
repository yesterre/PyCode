from pathlib import Path
import shutil
import uuid

from pycode.tools import ToolContext, search_code


def test_search_code_returns_path_line_and_text() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        project_path.mkdir()
        (project_path / "main.py").write_text(
            "class UserService:\n    pass\n",
            encoding="utf-8",
        )
        (project_path / "notes.txt").write_text("UserService in docs\n", encoding="utf-8")

        result = search_code(ToolContext(project_path), "userservice")

        assert result.ok is True
        assert result.data["matches"] == [
            {"path": "main.py", "line_number": 1, "line": "class UserService:"}
        ]
    finally:
        _cleanup(workspace)


def test_search_code_supports_include_globs_and_result_limit() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        project_path.mkdir()
        (project_path / "a.txt").write_text("target\n", encoding="utf-8")
        (project_path / "b.txt").write_text("target\n", encoding="utf-8")

        result = search_code(
            ToolContext(project_path),
            "target",
            include_globs=["*.txt"],
            max_results=1,
        )

        assert result.ok is True
        assert len(result.data["matches"]) == 1
        assert result.data["truncated"] is True
    finally:
        _cleanup(workspace)


def test_search_code_skips_excluded_directories() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        ignored = project_path / ".venv"
        ignored.mkdir(parents=True)
        (ignored / "ignored.py").write_text("target\n", encoding="utf-8")

        result = search_code(ToolContext(project_path), "target")

        assert result.ok is True
        assert result.data["matches"] == []
    finally:
        _cleanup(workspace)


def _workspace() -> Path:
    path = Path(".pytest_tmp_tools") / f"search_code_{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    return path.resolve()


def _cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
