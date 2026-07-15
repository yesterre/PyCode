from pathlib import Path

import pytest

from pycode.agent import AgentTask
from pycode.agent.llm_planner import (
    build_llm_next_action_prompt,
    build_llm_planner_prompt,
    parse_llm_next_action,
    parse_llm_plan,
    plan_next_action_with_llm,
    plan_task_with_llm,
)
from pycode.agent.prompts import build_agent_summary_context
from pycode.agent.types import AgentActionType
from pycode.tools import ToolSpec
from pycode.tools.base import success


def test_llm_planner_parses_valid_registered_tool_plan() -> None:
    llm = _MockPlannerLLM(
        """
        [
          {
            "tool": "query_graph",
            "arguments": {"query_type": "entry"},
            "reason": "Find entry candidates from the graph.",
            "required": false
          }
        ]
        """
    )
    task = AgentTask("这个项目的入口在哪里？", Path("."))

    result = plan_task_with_llm(
        task,
        tools={"query_graph": ToolSpec("query_graph", _fake_tool, True)},
        llm_client=llm,
    )

    assert result.source == "llm"
    assert [step.tool for step in result.steps] == ["query_graph"]
    assert result.steps[0].arguments == {"query_type": "entry"}
    assert "PyCode LLM Planner" in llm.prompts[0]


def test_parse_llm_plan_filters_unknown_tools_and_disallowed_tests() -> None:
    task = AgentTask("运行测试", Path("."), allow_tests=False)
    response = """
    [
      {"tool": "unknown_tool", "arguments": {}, "reason": "bad"},
      {"tool": "run_tests", "arguments": {}, "reason": "not allowed"},
      {"tool": "retrieve_context", "arguments": {"intent": "general"}, "reason": "ok"}
    ]
    """

    steps = parse_llm_plan(
        response,
        task=task,
        tools={
            "retrieve_context": ToolSpec("retrieve_context", _fake_tool, True),
            "run_tests": ToolSpec("run_tests", _fake_tool, read_only=False),
        },
    )

    assert [step.tool for step in steps] == ["retrieve_context"]


def test_llm_planner_result_records_filtered_tool_warnings() -> None:
    llm = _MockPlannerLLM(
        """
        [
          {"tool": "unknown_tool", "arguments": {}, "reason": "bad"},
          {"tool": "run_tests", "arguments": {}, "reason": "not allowed"},
          {"tool": "retrieve_context", "arguments": {}, "reason": "ok"}
        ]
        """
    )

    result = plan_task_with_llm(
        AgentTask("运行测试", Path("."), allow_tests=False),
        tools={
            "retrieve_context": ToolSpec("retrieve_context", _fake_tool, True),
            "run_tests": ToolSpec("run_tests", _fake_tool, read_only=False),
        },
        llm_client=llm,
    )

    assert [step.tool for step in result.steps] == ["retrieve_context"]
    assert result.warnings is not None
    assert any("unknown_tool" in warning for warning in result.warnings)
    assert any("run_tests" in warning for warning in result.warnings)


def test_parse_llm_plan_raises_when_no_usable_steps() -> None:
    with pytest.raises(ValueError, match="no usable"):
        parse_llm_plan(
            '[{"tool": "unknown_tool"}]',
            task=AgentTask("anything", Path(".")),
            tools={"retrieve_context": ToolSpec("retrieve_context", _fake_tool, True)},
        )


def test_llm_planner_prompt_contains_policy_and_tool_catalog() -> None:
    prompt = build_llm_planner_prompt(
        AgentTask("分析入口", Path(".")),
        tools={"retrieve_context": ToolSpec("retrieve_context", _fake_tool, True)},
        memory_index="# Memory",
    )

    assert "Return only a JSON array" in prompt
    assert "retrieve_context" in prompt
    assert "Only plan run_tests when allow_tests is true" in prompt
    assert "# Memory" in prompt


def test_parse_llm_next_action_parses_tool_call() -> None:
    action = parse_llm_next_action(
        """
        {
          "action_type": "tool_call",
          "tool_name": "retrieve_context",
          "arguments": {"intent": "general"},
          "reason": "Need current project evidence."
        }
        """,
        task=AgentTask("分析项目", Path(".")),
        tools={"retrieve_context": ToolSpec("retrieve_context", _fake_tool, True)},
    )

    assert action.type == AgentActionType.TOOL_CALL
    assert action.tool_call is not None
    assert action.tool_call.name == "retrieve_context"
    assert action.tool_call.arguments == {"intent": "general"}
    assert action.reason == "Need current project evidence."


def test_parse_llm_next_action_parses_final_answer() -> None:
    action = parse_llm_next_action(
        """
        {
          "action_type": "final_answer",
          "reason": "Evidence is sufficient.",
          "final_answer": "入口在 main.py。"
        }
        """,
        task=AgentTask("入口在哪里？", Path(".")),
        tools={},
    )

    assert action.type == AgentActionType.FINAL_ANSWER
    assert action.answer == "入口在 main.py。"


def test_parse_llm_next_action_parses_stop_with_error() -> None:
    action = parse_llm_next_action(
        """
        {
          "action_type": "stop_with_error",
          "reason": "Cannot continue safely.",
          "error": "No registered evidence tools are available."
        }
        """,
        task=AgentTask("入口在哪里？", Path(".")),
        tools={},
    )

    assert action.type == AgentActionType.STOP_WITH_ERROR
    assert action.error == "No registered evidence tools are available."


def test_parse_llm_next_action_rejects_unknown_tool() -> None:
    with pytest.raises(ValueError, match="unknown tool"):
        parse_llm_next_action(
            '{"action_type":"tool_call","tool_name":"missing","arguments":{}}',
            task=AgentTask("anything", Path(".")),
            tools={"retrieve_context": ToolSpec("retrieve_context", _fake_tool, True)},
        )


def test_parse_llm_next_action_rejects_json_array() -> None:
    with pytest.raises(ValueError, match="not an object"):
        parse_llm_next_action(
            '[{"action_type":"final_answer","final_answer":"bad shape"}]',
            task=AgentTask("anything", Path(".")),
            tools={},
        )


def test_parse_llm_next_action_rejects_invalid_arguments() -> None:
    spec = ToolSpec(
        "read_file",
        _fake_tool,
        True,
        input_schema={
            "type": "object",
            "properties": {"file_path": {"type": "string"}},
            "required": ["file_path"],
        },
    )

    with pytest.raises(ValueError, match="Missing required"):
        parse_llm_next_action(
            '{"action_type":"tool_call","tool_name":"read_file","arguments":{}}',
            task=AgentTask("read file", Path(".")),
            tools={"read_file": spec},
        )


def test_parse_llm_next_action_rejects_disallowed_run_tests() -> None:
    with pytest.raises(ValueError, match="tests were not allowed"):
        parse_llm_next_action(
            '{"action_type":"tool_call","tool_name":"run_tests","arguments":{}}',
            task=AgentTask("run tests", Path("."), allow_tests=False),
            tools={"run_tests": ToolSpec("run_tests", _fake_tool, read_only=False)},
        )


def test_plan_next_action_with_llm_uses_turn_context() -> None:
    llm = _MockPlannerLLM(
        """
        {
          "action_type": "final_answer",
          "reason": "The current context is enough.",
          "final_answer": "final from action"
        }
        """
    )
    task = AgentTask("入口在哪里？", Path("."))
    context = build_agent_summary_context(task, [], [], tools={})

    result = plan_next_action_with_llm(
        task,
        agent_context=context,
        tools={},
        llm_client=llm,
        turn_index=1,
        max_turns=4,
        steps=[],
        turns=[],
        tool_results=[],
        todos=[],
    )

    assert result.action.type == AgentActionType.FINAL_ANSWER
    assert result.action.answer == "final from action"
    assert "PyCode LLM Next-Action Planner" in llm.prompts[0]


def test_build_llm_next_action_prompt_contains_schema_and_context() -> None:
    task = AgentTask("入口在哪里？", Path("."))
    context = build_agent_summary_context(
        task,
        [],
        [],
        tools={"retrieve_context": ToolSpec("retrieve_context", _fake_tool, True)},
    )

    prompt = build_llm_next_action_prompt(
        task,
        agent_context=context,
        tools={"retrieve_context": ToolSpec("retrieve_context", _fake_tool, True)},
        turn_index=1,
        max_turns=4,
        steps=[],
        turns=[],
        tool_results=[],
        todos=[],
    )

    assert "Return only one JSON object" in prompt
    assert "action_type" in prompt
    assert "retrieve_context" in prompt
    assert "context_sections" in prompt


class _MockPlannerLLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


def _fake_tool(context, **kwargs):
    return success("fake", "ok")
