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
    event_types = [event.event_type for event in result.trace.events]
    assert "TurnStarted" in event_types
    assert "NextActionDecided" in event_types
    assert "PolicyDecision" in event_types
    assert "ObservationRecorded" in event_types
    assert "TurnFinished" in event_types
    assert "StopDecided" in event_types


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
    policy_events = [
        event for event in result.trace.events if event.event_type == "PolicyDecision"
    ]
    assert policy_events[-1].status == "denied"


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


def test_trace_records_llm_next_action_fallback() -> None:
    def retrieve_context(context: ToolContext, **kwargs):
        return success("retrieve_context", "Selected context.")

    result = run_agent_runtime(
        AgentTask("Where is the entry point?", Path(".")),
        RuntimeConfig(max_turns=3, enable_memory=False),
        tools={"retrieve_context": ToolSpec("retrieve_context", retrieve_context, True)},
        llm_client=_SequenceLLM(
            [
                "not an initial JSON plan",
                "not a next action",
                "still not a next action",
                "summary answer",
            ]
        ),
    )

    assert result.trace is not None
    events = {event.event_type: event for event in result.trace.events}
    assert "LLMNextActionStarted" in events
    assert "LLMNextActionSchemaFailed" in events
    assert "LLMNextActionFallback" in events
    next_action_events = [
        event for event in result.trace.events if event.event_type == "NextActionDecided"
    ]
    assert next_action_events[0].data["planner_source"] == "fallback"
    assert next_action_events[0].data["fallback_used"] is True
    assert next_action_events[0].data["schema_error"]


class _SequenceLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    def generate(self, prompt: str) -> str:
        if self.responses:
            return self.responses.pop(0)
        return "[]"
