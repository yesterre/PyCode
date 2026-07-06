from pathlib import Path
import shutil
import uuid

from pycode.tools import ToolContext
from pycode.tools.task_tools import task_dag


def test_task_dag_tool_creates_lists_and_gets_tasks() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        project_path.mkdir()
        context = ToolContext(project_path)

        created = task_dag(
            context,
            operation="create",
            task_id="task_001",
            title="Build index",
        )
        listed = task_dag(context, operation="list")
        loaded = task_dag(context, operation="get", task_id="task_001")

        assert created.ok is True
        assert created.data["task"]["id"] == "task_001"
        assert created.data["can_start"] is True
        assert listed.data["tasks"][0]["title"] == "Build index"
        assert loaded.data["task"]["id"] == "task_001"
        assert created.data["storage_dir"] == ".pclens/tasks"
    finally:
        _cleanup(workspace)


def test_task_dag_tool_claims_and_completes_ready_tasks() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        project_path.mkdir()
        context = ToolContext(project_path)
        task_dag(context, operation="create", task_id="task_001", title="Build index")
        task_dag(
            context,
            operation="create",
            task_id="task_002",
            title="Build graph",
            blocked_by=["task_001"],
        )

        claimed = task_dag(
            context,
            operation="claim",
            task_id="task_001",
            owner="codex",
        )
        completed = task_dag(context, operation="complete", task_id="task_001")

        assert claimed.ok is True
        assert claimed.data["task"]["owner"] == "codex"
        assert completed.ok is True
        assert [task["id"] for task in completed.data["ready_tasks"]] == ["task_002"]
    finally:
        _cleanup(workspace)


def test_task_dag_tool_rejects_blocked_claim() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        project_path.mkdir()
        context = ToolContext(project_path)
        task_dag(context, operation="create", task_id="task_001", title="Build index")
        task_dag(
            context,
            operation="create",
            task_id="task_002",
            title="Build graph",
            blocked_by="task_001",
        )

        result = task_dag(context, operation="claim", task_id="task_002")

        assert result.ok is False
        assert result.summary == "Task claim failed."
        assert "blocked" in (result.error or "")
    finally:
        _cleanup(workspace)


def test_task_dag_tool_requires_arguments() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        project_path.mkdir()
        context = ToolContext(project_path)

        missing_title = task_dag(context, operation="create")
        missing_task_id = task_dag(context, operation="get")
        unsupported = task_dag(context, operation="delete", task_id="task_001")

        assert missing_title.ok is False
        assert missing_title.summary == "Task title is required."
        assert missing_task_id.ok is False
        assert missing_task_id.summary == "Task id is required."
        assert unsupported.ok is False
        assert unsupported.summary == "Unsupported task operation."
    finally:
        _cleanup(workspace)


def test_task_dag_tool_writes_only_project_internal_task_files() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        project_path.mkdir()
        context = ToolContext(project_path)

        result = task_dag(
            context,
            operation="create",
            task_id="task_001",
            title="Build index",
        )

        assert result.ok is True
        assert (project_path / ".pclens" / "tasks" / "task_001.json").exists()
        assert not (workspace / "task_001.json").exists()
    finally:
        _cleanup(workspace)


def _workspace() -> Path:
    path = Path(".pytest_tmp_5c_unit") / f"task_tools_{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    return path.resolve()


def _cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
