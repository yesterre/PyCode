from pathlib import Path

from pycode.agent.todo import TodoManager
from pycode.agent.types import AgentStep
from pycode.tools import ToolContext
from pycode.tools.todo_write import todo_write


def test_todo_write_lists_current_runtime_todos() -> None:
    manager = TodoManager.from_steps([AgentStep("read_file")])
    context = ToolContext(Path("."), state={"todo_manager": manager})

    result = todo_write(context, operation="list")

    assert result.ok is True
    assert result.data["progress"]["total"] == 1
    assert result.data["todos"][0]["id"] == "todo-1"


def test_todo_write_updates_existing_todo_status() -> None:
    manager = TodoManager.from_steps([AgentStep("read_file")])
    context = ToolContext(Path("."), state={"todo_manager": manager})

    result = todo_write(
        context,
        operation="set_status",
        todo_id="todo-1",
        status="in_progress",
    )

    assert result.ok is True
    assert result.data["todo"]["status"] == "in_progress"
    assert manager.items[0].status == "in_progress"


def test_todo_write_requires_runtime_manager() -> None:
    result = todo_write(ToolContext(Path(".")), operation="list")

    assert result.ok is False
    assert result.summary == "Todo manager is unavailable."


def test_todo_write_rejects_invalid_status_and_unknown_id() -> None:
    manager = TodoManager.from_steps([AgentStep("read_file")])
    context = ToolContext(Path("."), state={"todo_manager": manager})

    invalid = todo_write(
        context,
        operation="set_status",
        todo_id="todo-1",
        status="blocked",
    )
    unknown = todo_write(
        context,
        operation="set_status",
        todo_id="missing",
        status="in_progress",
    )

    assert invalid.ok is False
    assert "Unsupported todo status" in invalid.error
    assert unknown.ok is False
    assert "Unknown todo id" in unknown.error


def test_todo_write_preserves_single_in_progress_constraint() -> None:
    manager = TodoManager.from_steps(
        [AgentStep("read_file"), AgentStep("search_code")]
    )
    context = ToolContext(Path("."), state={"todo_manager": manager})

    first = todo_write(
        context,
        operation="set_status",
        todo_id="todo-1",
        status="in_progress",
    )
    second = todo_write(
        context,
        operation="set_status",
        todo_id="todo-2",
        status="in_progress",
    )

    assert first.ok is True
    assert second.ok is False
    assert "already in progress" in second.error
