from pathlib import Path
import shutil
import uuid

import pytest

from pycode.agent.task_dag import TaskDAGStore, TaskStatus


def test_task_dag_creates_and_loads_task_files() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        project_path.mkdir()
        store = TaskDAGStore(project_path)

        created = store.create_task(
            task_id="task_001",
            title="Build index",
            description="Scan the project.",
        )
        loaded = store.get_task("task_001")

        assert created.id == "task_001"
        assert loaded.title == "Build index"
        assert (project_path / ".pclens" / "tasks" / "task_001.json").exists()
    finally:
        _cleanup(workspace)


def test_task_dag_lists_tasks_by_id() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        project_path.mkdir()
        store = TaskDAGStore(project_path)

        store.create_task(task_id="task_002", title="Second")
        store.create_task(task_id="task_001", title="First")

        assert [task.id for task in store.list_tasks()] == ["task_001", "task_002"]
    finally:
        _cleanup(workspace)


def test_task_dag_can_start_when_no_dependencies() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        project_path.mkdir()
        store = TaskDAGStore(project_path)
        task = store.create_task(task_id="task_001", title="Ready")

        result = store.can_start(task)

        assert result.can_start is True
        assert result.blocked_by == []
        assert result.missing_dependencies == []
    finally:
        _cleanup(workspace)


def test_task_dag_waits_for_dependencies_to_complete() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        project_path.mkdir()
        store = TaskDAGStore(project_path)
        store.create_task(task_id="task_001", title="Build index")
        dependent = store.create_task(
            task_id="task_002",
            title="Build graph",
            blocked_by=["task_001"],
        )

        blocked = store.can_start(dependent)
        store.claim_task("task_001")
        store.complete_task("task_001")
        ready = store.can_start("task_002")

        assert blocked.can_start is False
        assert blocked.blocked_by == ["task_001"]
        assert ready.can_start is True
    finally:
        _cleanup(workspace)


def test_task_dag_treats_missing_dependencies_as_blocking() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        project_path.mkdir()
        store = TaskDAGStore(project_path)
        task = store.create_task(
            task_id="task_001",
            title="Analyze impact",
            blocked_by=["missing"],
        )

        result = store.can_start(task)

        assert result.can_start is False
        assert result.missing_dependencies == ["missing"]
    finally:
        _cleanup(workspace)


def test_task_dag_claim_rejects_blocked_task() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        project_path.mkdir()
        store = TaskDAGStore(project_path)
        store.create_task(task_id="task_001", title="Build index")
        store.create_task(
            task_id="task_002",
            title="Build graph",
            blocked_by=["task_001"],
        )

        with pytest.raises(ValueError, match="blocked"):
            store.claim_task("task_002")
    finally:
        _cleanup(workspace)


def test_task_dag_complete_returns_newly_ready_downstream_tasks() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        project_path.mkdir()
        store = TaskDAGStore(project_path)
        store.create_task(task_id="task_001", title="Build index")
        store.create_task(
            task_id="task_002",
            title="Build graph",
            blocked_by=["task_001"],
        )

        store.claim_task("task_001")
        completed, ready_tasks = store.complete_task("task_001")

        assert completed.status == TaskStatus.COMPLETED
        assert [task.id for task in ready_tasks] == ["task_002"]
    finally:
        _cleanup(workspace)


def test_task_dag_rejects_completed_task_regression() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        project_path.mkdir()
        store = TaskDAGStore(project_path)
        store.create_task(task_id="task_001", title="Build index")

        store.claim_task("task_001")
        store.complete_task("task_001")

        with pytest.raises(ValueError, match="Completed task cannot be claimed"):
            store.claim_task("task_001")
    finally:
        _cleanup(workspace)


def test_task_dag_blocks_storage_outside_project() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        project_path.mkdir()
        outside = workspace / "outside_tasks"

        with pytest.raises(PermissionError, match="outside the project"):
            TaskDAGStore(project_path, tasks_dir=outside)
    finally:
        _cleanup(workspace)


def _workspace() -> Path:
    path = Path(".pytest_tmp_5c_unit") / f"task_dag_{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    return path.resolve()


def _cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
