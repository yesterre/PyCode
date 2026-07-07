from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pycode.agent.memory import MemoryItem
from pycode.agent.types import AgentStep, AgentTask
from pycode.llm_client import LLMClient
from pycode.storage import load_graph, load_index
from pycode.tools import ToolSpec


PLANNER_SOURCE_LLM = "llm"
PLANNER_SOURCE_RULE = "rule"
PLANNER_SOURCE_FALLBACK = "fallback"


@dataclass
class LLMPlannerResult:
    steps: list[AgentStep]
    source: str = PLANNER_SOURCE_LLM
    error: str | None = None
    raw_response: str = ""
    warnings: list[str] | None = None


def plan_task_with_llm(
    task: AgentTask,
    *,
    tools: dict[str, ToolSpec],
    llm_client: LLMClient,
    memory_index: str = "",
    relevant_memories: list[MemoryItem] | None = None,
) -> LLMPlannerResult:
    prompt = build_llm_planner_prompt(
        task,
        tools=tools,
        memory_index=memory_index,
        relevant_memories=relevant_memories or [],
    )
    raw_response = llm_client.generate(prompt)
    steps = parse_llm_plan(
        raw_response,
        task=task,
        tools=tools,
    )
    return LLMPlannerResult(
        steps=steps,
        raw_response=raw_response,
        warnings=_planner_warnings(raw_response, task=task, tools=tools),
    )


def build_llm_planner_prompt(
    task: AgentTask,
    *,
    tools: dict[str, ToolSpec],
    memory_index: str = "",
    relevant_memories: list[MemoryItem] | None = None,
) -> str:
    payload = {
        "task": {
            "description": task.description,
            "project_path": str(task.project_path),
            "allow_tests": task.allow_tests,
            "max_steps": task.max_steps,
            "graph_path": str(task.graph_path) if task.graph_path else None,
            "task_type": task.task_type,
        },
        "tools": _tool_catalog(tools),
        "project_summary": _project_summary(task),
        "policy": {
            "read_only_by_default": True,
            "do_not_modify_source": True,
            "do_not_commit_git": True,
            "run_tests_requires_allow_tests": True,
            "only_registered_tools_allowed": True,
        },
        "memory_index": memory_index,
        "relevant_memories": [
            {
                "name": memory.name,
                "type": memory.type,
                "description": memory.description,
                "path": memory.path,
                "body": memory.body[:800],
            }
            for memory in relevant_memories or []
        ],
    }
    return "\n\n".join(
        [
            "You are the PyCode LLM Planner.",
            "Your job is to plan tool calls only. Do not answer the user task directly.",
            "Do not claim any tool has already been executed.",
            "Use only registered tools from the provided catalog.",
            "Do not plan source-code writes, git commits, deletion commands, or dangerous operations.",
            "Only plan run_tests when allow_tests is true.",
            "Return only a JSON array. Do not wrap it in Markdown.",
            "Each JSON item must use this shape:",
            '{"tool": "query_graph", "arguments": {"query_type": "entry"}, "reason": "why this tool is useful", "required": false}',
            "Planning input:",
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        ]
    )


def parse_llm_plan(
    response: str,
    *,
    task: AgentTask,
    tools: dict[str, ToolSpec],
) -> list[AgentStep]:
    data = _loads_json_array(response)
    steps: list[AgentStep] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or "").strip()
        if not tool or tool not in tools:
            continue
        if tool == "run_tests" and not task.allow_tests:
            continue
        arguments = item.get("arguments", {})
        if not isinstance(arguments, dict):
            arguments = {}
        reason = str(item.get("reason") or f"LLM planner selected {tool}.")
        required = item.get("required", False)
        steps.append(
            AgentStep(
                tool=tool,
                arguments=dict(arguments),
                reason=reason,
                required=bool(required),
            )
        )
        if len(steps) >= task.max_steps:
            break
    if not steps:
        raise ValueError("LLM planner returned no usable tool steps.")
    return steps


def _loads_json_array(response: str) -> list[Any]:
    text = response.strip()
    if not text:
        raise ValueError("LLM planner response was empty.")
    if not text.startswith("["):
        match = re.search(r"\[[\s\S]*\]", text)
        if not match:
            raise ValueError("LLM planner response did not contain a JSON array.")
        text = match.group(0)
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("LLM planner JSON was not an array.")
    return data


def _planner_warnings(
    response: str,
    *,
    task: AgentTask,
    tools: dict[str, ToolSpec],
) -> list[str]:
    warnings: list[str] = []
    try:
        data = _loads_json_array(response)
    except ValueError:
        return warnings
    for item in data:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or "").strip()
        if tool and tool not in tools:
            warnings.append(f"LLM planner returned unknown tool and it was ignored: {tool}")
        if tool == "run_tests" and not task.allow_tests:
            warnings.append("LLM planner returned run_tests while tests were not allowed; it was ignored.")
    return warnings


def _tool_catalog(tools: dict[str, ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "name": spec.name,
            "read_only": spec.read_only,
            "writes_internal_state": spec.writes_internal_state,
        }
        for spec in tools.values()
    ]


def _project_summary(task: AgentTask) -> dict[str, Any]:
    project_path = Path(task.project_path)
    summary: dict[str, Any] = {
        "index_loaded": False,
        "graph_loaded": False,
    }
    index_path = project_path / ".pclens" / "index.json"
    graph_path = (
        Path(task.graph_path)
        if task.graph_path is not None
        else project_path / ".pclens" / "code_graph.json"
    )
    try:
        index = load_index(index_path)
        summary.update(
            {
                "index_loaded": True,
                "python_files": len(index.files),
                "imports": sum(len(file.imports) for file in index.files),
                "classes": sum(len(file.classes) for file in index.files),
                "functions": sum(len(file.functions) for file in index.files),
                "sample_files": [file.path for file in index.files[:20]],
            }
        )
    except (OSError, ValueError, KeyError):
        summary["index_path"] = str(index_path)

    try:
        graph = load_graph(graph_path)
        node_counts: dict[str, int] = {}
        edge_counts: dict[str, int] = {}
        for node in graph.nodes:
            node_counts[node.type] = node_counts.get(node.type, 0) + 1
        for edge in graph.edges:
            edge_counts[edge.type] = edge_counts.get(edge.type, 0) + 1
        summary.update(
            {
                "graph_loaded": True,
                "nodes": len(graph.nodes),
                "edges": len(graph.edges),
                "node_types": node_counts,
                "edge_types": edge_counts,
            }
        )
    except (OSError, ValueError, KeyError):
        summary["graph_path"] = str(graph_path)
    return summary
