from pathlib import Path

import pytest

from pycode.agent import AgentTask
from pycode.agent.llm_planner import (
    build_llm_planner_prompt,
    parse_llm_plan,
    plan_task_with_llm,
)
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


class _MockPlannerLLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


def _fake_tool(context, **kwargs):
    return success("fake", "ok")
