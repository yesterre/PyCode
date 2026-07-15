from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pycode.agent.memory import MemoryItem
from pycode.agent.types import (
    AgentAction,
    AgentActionType,
    AgentStep,
    AgentTask,
    AgentTurn,
    ToolCall,
)
from pycode.constants import DEFAULT_ARTIFACT_DIR, DEFAULT_GRAPH_FILE, DEFAULT_INDEX_FILE
from pycode.llm_client import LLMClient, LLMError, classify_llm_error
from pycode.storage import load_graph, load_index
from pycode.tools import ToolSpec
from pycode.tools.base import validate_tool_arguments
from pycode.utils import parse_json_array_response, parse_json_object_response

if TYPE_CHECKING:
    from pycode.agent.context import AgentContext
    from pycode.agent.todo import TodoItem
    from pycode.tools.base import ToolResult


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


@dataclass
class LLMNextActionResult:
    action: AgentAction
    source: str = PLANNER_SOURCE_LLM
    raw_response: str = ""
    error: str | None = None
    error_category: str | None = None
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


def plan_next_action_with_llm(
    task: AgentTask,
    *,
    agent_context: "AgentContext",
    tools: dict[str, ToolSpec],
    llm_client: LLMClient,
    turn_index: int,
    max_turns: int,
    steps: list[AgentStep],
    turns: list[AgentTurn],
    tool_results: list["ToolResult"],
    todos: list["TodoItem"],
) -> LLMNextActionResult:
    prompt = build_llm_next_action_prompt(
        task,
        agent_context=agent_context,
        tools=tools,
        turn_index=turn_index,
        max_turns=max_turns,
        steps=steps,
        turns=turns,
        tool_results=tool_results,
        todos=todos,
    )
    try:
        raw_response = llm_client.generate(prompt)
    except Exception as exc:
        category = classify_llm_error(exc)
        raise LLMError(
            f"LLM next-action call failed ({category}): {exc}",
            category=category,
        ) from exc
    action = parse_llm_next_action(raw_response, task=task, tools=tools)
    return LLMNextActionResult(action=action, raw_response=raw_response)


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
                "id": memory.id,
                "type": memory.type,
                "title": memory.title,
                "summary": memory.summary,
                "confidence": memory.confidence,
                "related_files": memory.related_files,
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
            "Each tool arguments object must strictly use property names from that tool's input_schema.properties.",
            "Do not invent argument aliases. For read_file use file_path, not path.",
            (
                "Memory is supporting context, not current code evidence. For code-fact tasks "
                "about project functionality, core files, entry points, dependencies, impact, "
                "or implementation details, do not plan only memory; include at least one "
                "current-code evidence tool such as retrieve_context, query_graph, search_code, "
                "git_diff, changed_files, or read_file."
            ),
            "Return only a JSON array. Do not wrap it in Markdown.",
            "Each JSON item must use this shape:",
            '{"tool": "query_graph", "arguments": {"query_type": "entry"}, "reason": "why this tool is useful", "required": false}',
            "Planning input:",
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        ]
    )


def build_llm_next_action_prompt(
    task: AgentTask,
    *,
    agent_context: "AgentContext",
    tools: dict[str, ToolSpec],
    turn_index: int,
    max_turns: int,
    steps: list[AgentStep],
    turns: list[AgentTurn],
    tool_results: list["ToolResult"],
    todos: list["TodoItem"],
) -> str:
    payload = {
        "task": {
            "description": task.description,
            "project_path": str(task.project_path),
            "allow_tests": task.allow_tests,
            "max_steps": task.max_steps,
            "task_type": task.task_type,
        },
        "turn": {
            "turn_index": turn_index,
            "max_turns": max_turns,
            "executed_turns": len(turns),
            "tool_results": len(tool_results),
        },
        "tools": _tool_catalog(tools),
        "steps": [
            {
                "index": index,
                "tool": step.tool,
                "arguments": step.arguments,
                "reason": step.reason,
                "required": step.required,
                "todo_id": step.todo_id,
            }
            for index, step in enumerate(steps, start=1)
        ],
        "recent_tool_results": [
            {
                "tool": result.tool,
                "ok": result.ok,
                "summary": result.summary,
                "error": result.error,
                "data": result.data,
            }
            for result in tool_results[-5:]
        ],
        "todos": [
            {
                "id": todo.id,
                "tool": todo.tool,
                "status": todo.status,
                "title": todo.title,
                "reason": todo.reason,
                "error": todo.error,
            }
            for todo in todos
        ],
        "context_sections": [
            {
                "name": section.name,
                "placement": section.placement,
                "source": section.source,
                "priority": section.priority,
                "included": section.included,
                "reason": section.reason,
                "content": section.content,
            }
            for section in agent_context.sections
        ],
        "skipped_context_sections": [
            {"name": section.name, "reason": section.reason}
            for section in agent_context.skipped_sections
        ],
        "context_warnings": list(agent_context.warnings),
        "response_schema": {
            "action_type": "tool_call | final_answer | stop_with_error | no_op",
            "tool_name": "required only for tool_call",
            "arguments": "object, required only for tool_call",
            "reason": "brief reason for the decision",
            "final_answer": "answer text for final_answer, otherwise null",
            "stop_reason": "optional stop reason",
            "error": "required for stop_with_error when reason is not enough",
        },
    }
    return "\n\n".join(
        [
            "You are the PyCode LLM Next-Action Planner.",
            "Your job is to choose exactly one next Agent action for the current turn.",
            "Use only evidence visible in the provided context and tool results.",
            "If evidence is insufficient and a useful registered tool exists, choose tool_call.",
            "If evidence is sufficient, choose final_answer.",
            "If the task cannot continue safely or no useful action exists, choose stop_with_error or no_op.",
            "Use only registered tools from the catalog. Do not invent tool names or argument names.",
            "Do not request source-code writes, file deletion, git commits, git reset, or dangerous operations.",
            "Only choose run_tests when allow_tests is true.",
            "Return only one JSON object. Do not wrap it in Markdown.",
            "Required JSON shape:",
            (
                '{"action_type":"tool_call","tool_name":"retrieve_context",'
                '"arguments":{},"reason":"why this action is next",'
                '"final_answer":null,"stop_reason":null,"error":null}'
            ),
            "Next-action input:",
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        ]
    )


def parse_llm_next_action(
    response: str,
    *,
    task: AgentTask,
    tools: dict[str, ToolSpec],
) -> AgentAction:
    data = parse_json_object_response(response)
    action_type = str(data.get("action_type") or "").strip()
    reason = str(data.get("reason") or "").strip()

    if action_type not in {item.value for item in AgentActionType}:
        raise ValueError(f"LLM next-action returned unsupported action_type: {action_type}")

    if action_type == AgentActionType.TOOL_CALL:
        tool_name = str(data.get("tool_name") or "").strip()
        if not tool_name:
            raise ValueError("LLM next-action tool_call is missing tool_name.")
        if tool_name not in tools:
            raise ValueError(f"LLM next-action returned unknown tool: {tool_name}")
        if tool_name == "run_tests" and not task.allow_tests:
            raise ValueError("LLM next-action returned run_tests while tests were not allowed.")
        arguments = data.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError("LLM next-action arguments must be an object.")
        argument_error = validate_tool_arguments(tools[tool_name], arguments)
        if argument_error is not None:
            raise ValueError(
                f"LLM next-action returned invalid arguments for {tool_name}: {argument_error}"
            )
        return AgentAction.tool(
            ToolCall(
                name=tool_name,
                arguments=dict(arguments),
                reason=reason,
            ),
            reason=reason or f"LLM selected tool {tool_name}.",
        )

    if action_type == AgentActionType.FINAL_ANSWER:
        answer = data.get("final_answer")
        answer_text = str(answer).strip() if answer is not None else None
        if not reason and not answer_text:
            raise ValueError("LLM final_answer requires reason or final_answer text.")
        return AgentAction.final(reason=reason, answer=answer_text)

    if action_type == AgentActionType.STOP_WITH_ERROR:
        error = str(data.get("error") or "").strip()
        if not reason and not error:
            raise ValueError("LLM stop_with_error requires reason or error.")
        return AgentAction.stop_error(error or reason, reason=reason)

    return AgentAction.no_op(reason=reason or "LLM selected no_op.")


def parse_llm_plan(
    response: str,
    *,
    task: AgentTask,
    tools: dict[str, ToolSpec],
) -> list[AgentStep]:
    data = parse_json_array_response(response)
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
        argument_error = validate_tool_arguments(tools[tool], arguments)
        if argument_error is not None:
            raise ValueError(
                f"LLM planner returned invalid arguments for {tool}: {argument_error}"
            )
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


def _planner_warnings(
    response: str,
    *,
    task: AgentTask,
    tools: dict[str, ToolSpec],
) -> list[str]:
    warnings: list[str] = []
    try:
        data = parse_json_array_response(response)
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
            "description": spec.description,
            "input_schema": spec.input_schema,
            "examples": spec.examples,
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
    index_path = project_path / DEFAULT_ARTIFACT_DIR / DEFAULT_INDEX_FILE
    graph_path = (
        Path(task.graph_path)
        if task.graph_path is not None
        else project_path / DEFAULT_ARTIFACT_DIR / DEFAULT_GRAPH_FILE
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
