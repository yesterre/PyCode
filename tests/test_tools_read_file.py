from pathlib import Path
import shutil
import uuid

from pycode.tools import ToolContext, read_file


def test_read_file_reads_project_local_file_with_line_range() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        project_path.mkdir()
        (project_path / "main.py").write_text("line1\nline2\nline3", encoding="utf-8")

        result = read_file(ToolContext(project_path), "main.py", start_line=2, end_line=3)

        assert result.ok is True
        assert result.data["path"] == "main.py"
        assert result.data["content"] == "line2\nline3"
        assert result.data["line_count"] == 3
    finally:
        _cleanup(workspace)


def test_read_file_blocks_paths_outside_project() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        project_path.mkdir()
        outside = workspace / "outside.py"
        outside.write_text("secret", encoding="utf-8")

        result = read_file(ToolContext(project_path), outside)

        assert result.ok is False
        assert result.summary == "File read denied."
        assert "outside the project" in (result.error or "")
    finally:
        _cleanup(workspace)


def test_read_file_reports_missing_file() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        project_path.mkdir()

        result = read_file(ToolContext(project_path), "missing.py")

        assert result.ok is False
        assert result.summary == "File does not exist."
    finally:
        _cleanup(workspace)


def _workspace() -> Path:
    path = Path(".pytest_tmp_tools") / f"read_file_{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    return path.resolve()


def _cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
