"""Regression coverage for state-aware planned Step binding."""

import json
from pathlib import Path

import pytest

from pycode.agent.runtime import _CompletedPlannedAction, _start_step_todo, run_agent_runtime
from pycode.agent.todo import TodoManager, TodoStatus
from pycode.agent.types import AgentStep, AgentStopReason, AgentTask, RuntimeConfig
from pycode.tools import TOOLS, ToolContext, ToolSpec
from pycode.tools.base import success


class Replies:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.calls = 0

    def generate(self, prompt: str) -> str:
        self.calls += 1
        return self.responses.pop(0)


def plan(*tools: str) -> str:
    return json.dumps([
        {"tool": tool, "arguments": {}, "reason": f"Use {tool}.", "required": False}
        for tool in tools
    ])


def action(tool: str, **arguments) -> str:
    return json.dumps({
        "action_type": "tool_call", "tool_name": tool,
        "arguments": arguments, "reason": f"Use {tool}.",
    })


FINAL = json.dumps({"action_type": "final_answer", "reason": "Done.", "final_answer": "done"})


def run(replies: Replies, tools: dict[str, ToolSpec], max_turns: int = 4):
    return run_agent_runtime(
        AgentTask("Inspect repository", Path(".")),
        RuntimeConfig(max_turns=max_turns, enable_memory=False),
        tools=tools, llm_client=replies,
    )


def recording_tools(*names: str):
    calls: list[str] = []
    specs = {}
    for name in names:
        def handler(context: ToolContext, _name=name, **kwargs):
            calls.append(_name)
            return success(_name, f"Used {_name}.")
        specs[name] = ToolSpec(name, handler, True)
    return specs, calls


def test_distinct_planned_tools_execute_in_order() -> None:
    tools, calls = recording_tools("search_code", "read_file")
    result = run(Replies(plan("search_code", "read_file"),
                         action("search_code"), action("read_file"), FINAL), tools)

    assert calls == ["search_code", "read_file"]
    assert [todo.status for todo in result.todos] == [TodoStatus.COMPLETED] * 2
    assert result.stop_reason == AgentStopReason.FINAL


def test_two_same_name_steps_bind_to_two_distinct_pending_todos() -> None:
    tools, calls = recording_tools("read_file")
    result = run(Replies(plan("read_file", "read_file"),
                         action("read_file"), action("read_file"), FINAL), tools)

    assert calls == ["read_file", "read_file"]
    assert [todo.id for todo in result.todos] == ["todo-1", "todo-2"]
    assert [todo.status for todo in result.todos] == [TodoStatus.COMPLETED] * 2
    assert result.stop_reason == AgentStopReason.FINAL


def test_out_of_order_completed_step_does_not_hide_earlier_pending_step() -> None:
    tools, calls = recording_tools("search_code", "read_file")
    result = run(Replies(plan("search_code", "read_file"),
                         action("read_file"), action("search_code"), FINAL), tools)

    assert calls == ["read_file", "search_code"]
    assert [todo.status for todo in result.todos] == [TodoStatus.COMPLETED] * 2
    assert result.stop_reason == AgentStopReason.FINAL


def test_repeated_completed_planned_action_stops_before_second_tool_call() -> None:
    tools, calls = recording_tools("read_file")
    replies = Replies(plan("read_file"), action("read_file"), action("read_file"))
    result = run(replies, tools)

    assert calls == ["read_file"]
    assert replies.calls == 3  # Plan and two decisions; no additional LLM decision.
    assert result.stop_reason == AgentStopReason.ERROR
    assert result.planner_error == "Completed planned todo cannot be selected: todo-1"
    assert result.todos[0].status == TodoStatus.COMPLETED
    assert len(result.tool_results) == 1


def test_todo_write_early_completion_prevents_planned_tool_execution() -> None:
    tools, calls = recording_tools("read_file")
    tools["todo_write"] = TOOLS["todo_write"]
    replies = Replies(
        plan("todo_write", "read_file"),
        action("todo_write", operation="set_status", todo_id="todo-2", status="completed"),
        action("read_file"),
    )
    result = run(replies, tools)

    assert calls == []
    assert [todo.status for todo in result.todos] == [TodoStatus.COMPLETED] * 2
    assert result.stop_reason == AgentStopReason.ERROR
    assert result.planner_error == "Completed planned todo cannot be selected: todo-2"


def test_plan_external_tool_still_runs_without_touching_planned_todo() -> None:
    tools, calls = recording_tools("read_file", "search_code")
    result = run(Replies(plan("read_file"), action("search_code"), FINAL), tools)

    assert calls == ["search_code"]
    assert result.todos[0].status == TodoStatus.PENDING
    assert result.stop_reason == AgentStopReason.FINAL


def test_new_arguments_keep_same_name_plan_external_call_available() -> None:
    tools, calls = recording_tools("read_file")
    result = run(Replies(
        plan("read_file"),
        action("read_file", file_path="first.py"),
        action("read_file", file_path="second.py"),
        FINAL,
    ), tools)

    assert calls == ["read_file", "read_file"]
    assert len(result.tool_results) == 2
    assert result.todos[0].status == TodoStatus.COMPLETED
    assert result.stop_reason == AgentStopReason.FINAL


def test_execute_entry_rejects_completed_todo_before_manager_start(monkeypatch) -> None:
    step = AgentStep("read_file")
    manager = TodoManager.from_steps([step])
    manager.complete(step.todo_id)
    calls = []
    monkeypatch.setattr(manager, "start", lambda todo_id: calls.append(todo_id))

    with pytest.raises(_CompletedPlannedAction, match="todo-1"):
        _start_step_todo(manager, step, None)
    assert calls == []


def test_completed_to_in_progress_remains_illegal() -> None:
    manager = TodoManager.from_steps([AgentStep("read_file")])
    manager.complete("todo-1")
    with pytest.raises(ValueError, match="Completed todo cannot move to in_progress"):
        manager.start("todo-1")
