from io import StringIO
from pathlib import Path

import pytest

from pycode.models import CodeGraph, FileInfo, GraphEdge, GraphNode, ProjectIndex
from pycode.agent import AgentTask, RuntimeConfig, run_agent_runtime
from pycode.rich_output import (
    make_console,
    print_agent_result_rich,
    print_graph_summary_rich,
    print_index_summary_rich,
    print_query_result_rich,
    rich_available,
)
from pycode.tools import ToolContext, ToolSpec
from pycode.tools.base import success


pytest.importorskip("rich")


def test_rich_index_summary_prints_project_metrics() -> None:
    index = ProjectIndex(
        project_path="demo_project",
        files=[
            FileInfo(
                path="main.py",
                imports=["services.user_service"],
                functions=["main"],
            )
        ],
    )
    output = StringIO()

    assert print_index_summary_rich(
        index,
        Path(".pclens/index.json"),
        console=make_console(output),
    )

    text = output.getvalue()
    assert rich_available()
    assert "PyCode Index Completed" in text
    assert "Python files" in text
    assert "main.py" in text


def test_rich_graph_and_query_output_prints_edges() -> None:
    graph = CodeGraph(
        project_path="demo_project",
        nodes=[
            GraphNode("file:main.py", "file", "main.py", "main.py"),
            GraphNode(
                "file:services/user_service.py",
                "file",
                "user_service.py",
                "services/user_service.py",
            ),
        ],
        edges=[
            GraphEdge(
                "file:main.py",
                "file:services/user_service.py",
                "imports",
            )
        ],
    )
    output = StringIO()
    console = make_console(output)

    assert print_graph_summary_rich(graph, Path(".pclens/code_graph.json"), console=console)
    assert print_query_result_rich(
        "imports",
        graph.edges,
        Path(".pclens/code_graph.json"),
        console=console,
    )

    text = output.getvalue()
    assert "PyCode Graph Completed" in text
    assert "PyCode Query Completed" in text
    assert "file:main.py" in text
    assert "file:services/user_service.py" in text


def test_rich_agent_output_prints_planner_events() -> None:
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
    output = StringIO()

    assert print_agent_result_rich(result, console=make_console(output))

    text = output.getvalue()
    assert "Planner Events" in text
    assert "LLMNextActionFallback" in text


def test_rich_agent_output_prints_todo_trace_context_and_non_tool_turns() -> None:
    def retrieve_context(context: ToolContext, **kwargs):
        return success(
            "retrieve_context",
            "Selected context.",
            data={"items": [{"path": "main.py", "content": "def main(): pass"}]},
            evidence=["main.py"],
        )

    result = run_agent_runtime(
        AgentTask("Where is the entry point?", Path(".")),
        RuntimeConfig(max_turns=3, enable_memory=False),
        tools={"retrieve_context": ToolSpec("retrieve_context", retrieve_context, True)},
        llm_client=_SequenceLLM(
            [
                "not an initial JSON plan",
                '{"action_type":"final_answer","reason":"Enough evidence","final_answer":"main.py is the entry point."}',
            ]
        ),
    )
    output = StringIO()

    assert print_agent_result_rich(result, show_context=True, console=make_console(output))

    text = output.getvalue()
    assert "Runtime Turns" in text
    assert "final_answer" in text
    assert "Todos" in text
    assert "Trace" in text
    assert "Context Sections" in text
    assert "Planner Events" in text
    assert "Planner fallback reason" in text
    assert "ValueError" in text
    assert "LLMNextActionFinished" in text


class _SequenceLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    def generate(self, prompt: str) -> str:
        if self.responses:
            return self.responses.pop(0)
        return "[]"
