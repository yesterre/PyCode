from pathlib import Path

from pycode.agent import AgentTask, RuntimeConfig, run_agent_runtime
from pycode.tools import ToolContext, ToolSpec
from pycode.tools.base import success


def test_trace_records_successful_tool_lifecycle() -> None:
    def retrieve_context(context: ToolContext, **kwargs):
        return success("retrieve_context", "Selected context.", evidence=["main.py"])

    result = run_agent_runtime(
        AgentTask("Where is the entry point?", Path(".")),
        RuntimeConfig(max_turns=1),
        tools={"retrieve_context": ToolSpec("retrieve_context", retrieve_context, True)},
    )

    assert result.trace is not None
    assert result.trace.stop_reason == "final"
    assert result.trace.duration_ms is not None
    assert [tool.tool for tool in result.trace.tools] == ["retrieve_context"]
    tool_trace = result.trace.tools[0]
    assert tool_trace.status == "ok"
    assert tool_trace.duration_ms is not None
    assert tool_trace.summary == "Selected context."
    assert tool_trace.result_data["evidence"] == ["main.py"]
    assert result.trace.to_dict()["tools"][0]["status"] == "ok"


def test_trace_records_tool_exceptions_as_failures() -> None:
    def retrieve_context(context: ToolContext, **kwargs):
        raise RuntimeError("bad tool")

    result = run_agent_runtime(
        AgentTask("Where is the entry point?", Path(".")),
        RuntimeConfig(max_turns=1),
        tools={"retrieve_context": ToolSpec("retrieve_context", retrieve_context, True)},
    )

    assert result.trace is not None
    tool_trace = result.trace.tools[0]
    assert tool_trace.status == "failed"
    assert tool_trace.summary == "Tool execution raised an exception."
    assert "RuntimeError: bad tool" in (tool_trace.error or "")


def test_trace_records_policy_denials() -> None:
    def retrieve_context(context: ToolContext, **kwargs):
        return success("retrieve_context", "should not run")

    result = run_agent_runtime(
        AgentTask("Where is the entry point?", Path("."), allow_tests=False),
        RuntimeConfig(max_turns=1),
        tools={"retrieve_context": ToolSpec("retrieve_context", retrieve_context, False)},
    )

    assert result.trace is not None
    tool_trace = result.trace.tools[0]
    assert tool_trace.status == "denied"
    assert tool_trace.denied_by == "policy"
    assert result.tool_results[0].data["denied"] is True


def test_trace_summarizes_large_arguments_and_results() -> None:
    large_text = "x" * 1000

    def retrieve_context(context: ToolContext, **kwargs):
        return success("retrieve_context", "Selected context.", content=large_text)

    result = run_agent_runtime(
        AgentTask(large_text, Path(".")),
        RuntimeConfig(max_turns=1),
        tools={"retrieve_context": ToolSpec("retrieve_context", retrieve_context, True)},
    )

    assert result.trace is not None
    tool_trace = result.trace.tools[0]
    assert len(tool_trace.arguments["question"]) < len(large_text)
    assert tool_trace.arguments["question"].endswith("...[truncated]")
    assert len(tool_trace.result_data["content"]) < len(large_text)
    assert tool_trace.result_data["content"].endswith("...[truncated]")
