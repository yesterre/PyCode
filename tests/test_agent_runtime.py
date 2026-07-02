from pathlib import Path

from pycode.agent import (
    AgentTask,
    RuntimeConfig,
    execute_tool_call,
    run_agent_runtime,
)
from pycode.agent.types import AgentStopReason, ToolCall
from pycode.tools import ToolContext, ToolSpec
from pycode.tools.base import success


def test_runtime_executes_one_tool_per_turn_and_records_messages() -> None:
    calls: list[dict] = []

    def retrieve_context(context: ToolContext, **kwargs):
        calls.append(kwargs)
        return success("retrieve_context", "Selected context.", evidence=["main.py"])

    task = AgentTask(
        "\u8fd9\u4e2a\u9879\u76ee\u7684\u5165\u53e3\u5728\u54ea\uff1f",
        Path("."),
    )
    result = run_agent_runtime(
        task,
        RuntimeConfig(max_turns=4),
        tools={"retrieve_context": ToolSpec("retrieve_context", retrieve_context, True)},
    )

    assert result.stop_reason == AgentStopReason.FINAL
    assert [turn.tool_call.name for turn in result.turns] == ["retrieve_context"]
    assert calls[0]["intent"] == "entry"
    assert [message.role for message in result.messages] == ["user", "assistant", "tool"]
    assert result.tool_results[0].data["evidence"] == ["main.py"]


def test_runtime_plan_only_skips_tools_and_llm() -> None:
    def raising_tool(context: ToolContext, **kwargs):
        raise AssertionError("tool should not run")

    task = AgentTask(
        "\u8fd9\u4e2a\u9879\u76ee\u7684\u9605\u8bfb\u987a\u5e8f\u662f\u4ec0\u4e48\uff1f",
        Path("."),
    )
    result = run_agent_runtime(
        task,
        RuntimeConfig(plan_only=True),
        tools={"retrieve_context": ToolSpec("retrieve_context", raising_tool, True)},
        llm_client=_MockLLM("should not be called"),
    )

    assert result.stop_reason == AgentStopReason.PLAN_ONLY
    assert result.tool_results == []
    assert result.turns == []
    assert [step.tool for step in result.steps] == ["retrieve_context"]


def test_runtime_stops_when_max_turns_is_reached() -> None:
    def retrieve_context(context: ToolContext, **kwargs):
        return success("retrieve_context", "Selected context.")

    task = AgentTask(
        "\u8fd9\u4e2a\u9879\u76ee\u7684\u5165\u53e3\u5728\u54ea\uff1f"
        "\u9605\u8bfb\u987a\u5e8f\u5e94\u8be5\u662f\u600e\u6837\u7684\uff1f",
        Path("."),
    )
    result = run_agent_runtime(
        task,
        RuntimeConfig(max_turns=1),
        tools={"retrieve_context": ToolSpec("retrieve_context", retrieve_context, True)},
    )

    assert result.stop_reason == AgentStopReason.MAX_TURNS
    assert len(result.steps) == 2
    assert len(result.turns) == 1


def test_execute_tool_call_uses_policy_for_non_read_only_tools() -> None:
    def run_tests(context: ToolContext):
        return success("run_tests", "should not run")

    result = execute_tool_call(
        AgentTask("run tests", Path("."), allow_tests=False),
        ToolCall("run_tests"),
        tools={"run_tests": ToolSpec("run_tests", run_tests, read_only=False)},
    )

    assert result.ok is False
    assert result.summary == "Tool execution denied."


class _MockLLM:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answer
