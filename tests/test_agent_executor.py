from pathlib import Path

from pycode.agent import AgentStep, AgentTask, execute_plan, run_agent_task
from pycode.tools import ToolContext, ToolResult, ToolSpec
from pycode.tools.base import success


def test_execute_plan_calls_registered_tools_in_order() -> None:
    calls: list[tuple[str, dict]] = []

    def first_tool(context: ToolContext, **kwargs):
        calls.append(("first", kwargs))
        return success("first", "first ok", value=1)

    def second_tool(context: ToolContext, **kwargs):
        calls.append(("second", kwargs))
        return success("second", "second ok", value=2)

    task = AgentTask("demo task", Path("."))
    steps = [
        AgentStep("first", {"target": "main.py"}, "read first"),
        AgentStep("second", {}, "read second"),
    ]
    tools = {
        "first": ToolSpec("first", first_tool, read_only=True),
        "second": ToolSpec("second", second_tool, read_only=True),
    }

    results = execute_plan(task, steps, tools=tools, context=ToolContext(Path(".")))

    assert calls == [("first", {"target": "main.py"}), ("second", {})]
    assert [result.summary for result in results] == ["first ok", "second ok"]


def test_execute_plan_denies_non_read_only_tool_when_tests_not_allowed() -> None:
    def write_like_tool(context: ToolContext):
        return success("run_tests", "should not run")

    task = AgentTask("run tests", Path("."), allow_tests=False)
    steps = [AgentStep("run_tests")]
    tools = {
        "run_tests": ToolSpec("run_tests", write_like_tool, read_only=False),
    }

    results = execute_plan(task, steps, tools=tools, context=ToolContext(Path(".")))

    assert results[0].ok is False
    assert results[0].summary == "Tool execution denied."


def test_execute_plan_records_unknown_tool() -> None:
    task = AgentTask("demo task", Path("."))

    results = execute_plan(task, [AgentStep("missing")], tools={})

    assert results[0].ok is False
    assert results[0].summary == "Unknown tool."


def test_run_agent_task_builds_prompt_and_calls_optional_llm() -> None:
    def changed_files(context: ToolContext):
        return success("changed_files", "Found 1 changed files.", files=["main.py"])

    def git_diff(context: ToolContext):
        return success("git_diff", "Git diff collected.", diff="diff --git a/main.py")

    llm = _MockLLM("agent answer")
    result = run_agent_task(
        "分析当前 git diff",
        Path("."),
        llm_client=llm,
        tools={
            "changed_files": ToolSpec("changed_files", changed_files, read_only=True),
            "git_diff": ToolSpec("git_diff", git_diff, read_only=True),
        },
    )

    assert result.answer == "agent answer"
    assert result.tool_results[0].data["files"] == ["main.py"]
    assert "User task: 分析当前 git diff" in result.prompt
    assert "Found 1 changed files." in llm.prompts[-1]


class _MockLLM:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answer
