import pytest

from pycode.agent.todo import TodoList, TodoManager, TodoStatus
from pycode.agent.types import AgentStep


def test_todo_list_from_steps_assigns_stable_ids() -> None:
    steps = [
        AgentStep("read_file", reason="Read target file.", required=False),
        AgentStep("search_code", reason="Search keyword."),
    ]

    todo_list = TodoList.from_steps(steps)

    assert [step.todo_id for step in steps] == ["todo-1", "todo-2"]
    assert [item.id for item in todo_list.items] == ["todo-1", "todo-2"]
    assert todo_list.items[0].title == "Read target file."
    assert todo_list.items[0].required is False


def test_todo_manager_tracks_pending_in_progress_completed() -> None:
    manager = TodoManager.from_steps([AgentStep("retrieve_context")])

    started = manager.start("todo-1")
    completed = manager.complete("todo-1")

    assert started.id == "todo-1"
    assert completed.status == TodoStatus.COMPLETED
    assert manager.summary() == {
        "total": 1,
        "pending": 0,
        "in_progress": 0,
        "completed": 1,
        "failed": 0,
        "current": None,
    }


def test_todo_manager_records_failed_error() -> None:
    manager = TodoManager.from_steps([AgentStep("git_diff")])

    manager.start("todo-1")
    failed = manager.fail("todo-1", "git repository unavailable")

    assert failed.status == TodoStatus.FAILED
    assert failed.error == "git repository unavailable"
    assert manager.summary()["failed"] == 1


def test_todo_manager_allows_only_one_in_progress() -> None:
    manager = TodoManager.from_steps(
        [AgentStep("read_file"), AgentStep("search_code")]
    )

    manager.start("todo-1")

    with pytest.raises(ValueError, match="already in progress"):
        manager.start("todo-2")


def test_todo_manager_rejects_invalid_status_and_unknown_id() -> None:
    manager = TodoManager.from_steps([AgentStep("read_file")])

    with pytest.raises(ValueError, match="Unsupported todo status"):
        manager.set_status("todo-1", "blocked")

    with pytest.raises(ValueError, match="Unknown todo id"):
        manager.start("missing")


def test_todo_manager_to_dict_is_stable() -> None:
    manager = TodoManager.from_steps([AgentStep("read_file", reason="Read file.")])

    assert manager.to_dict() == [
        {
            "id": "todo-1",
            "title": "Read file.",
            "tool": "read_file",
            "reason": "Read file.",
            "status": "pending",
            "error": None,
            "step_index": 1,
            "required": True,
        }
    ]
