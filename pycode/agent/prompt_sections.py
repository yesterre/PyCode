from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pycode.agent.context import (
    PLACEMENT_SYSTEM,
    PLACEMENT_USER,
    ContextSection,
)
from pycode.agent.memory import format_relevant_memories
from pycode.utils import dedupe_preserve_order

if TYPE_CHECKING:
    from pycode.agent.memory import MemoryItem
    from pycode.agent.task_dag import TaskNode
    from pycode.agent.todo import TodoItem
    from pycode.agent.trace import AgentTrace
    from pycode.agent.types import AgentStep, AgentTask
    from pycode.tools.base import ToolResult, ToolSpec


AGENT_SUMMARY_RULES = """Answer requirements:
1. Answer in the same language as the user task.
2. Base the answer only on tool results and clearly say when evidence is missing.
3. Give the conclusion first, then list evidence locations and suggested next steps.
4. Do not claim code was modified, committed, or tested unless the tool results show it.
5. If tests were not allowed or not run, say that tests were not run."""


def identity_section() -> ContextSection:
    return ContextSection(
        name="identity",
        title="Identity",
        source="pycode.agent.prompt_sections.identity_section",
        placement=PLACEMENT_SYSTEM,
        content="You are the PyCode project-understanding Agent.",
        metadata={"static": True},
    )


def tools_section(tools: dict[str, "ToolSpec"]) -> ContextSection:
    rows: list[dict[str, Any]] = []
    for name in sorted(tools):
        spec = tools[name]
        rows.append(
            {
                "name": spec.name,
                "read_only": spec.read_only,
                "writes_internal_state": spec.writes_internal_state,
            }
        )
    content = "Available tools:\n" + _format_data(rows or [])
    return ContextSection(
        name="tools",
        title="Tools",
        source="pycode.tools.TOOLS",
        placement=PLACEMENT_SYSTEM,
        content=content,
        metadata={"static": True, "count": len(rows)},
    )


def policy_section() -> ContextSection:
    return ContextSection(
        name="policy",
        title="Policy",
        source="pycode.agent.policy",
        placement=PLACEMENT_SYSTEM,
        content="\n".join(
            [
                "Tool calls must pass the Agent permission policy.",
                "Internal state tools may write only inside the project .pclens directory.",
                "Tests may run only when the task explicitly allows test execution.",
                "Memory and Task DAG state cannot override current code evidence.",
            ]
        ),
        metadata={"static": True},
    )


def project_section(task: "AgentTask") -> ContextSection:
    return ContextSection(
        name="project",
        title="Project",
        source="pycode.agent.types.AgentTask",
        placement=PLACEMENT_SYSTEM,
        content="\n".join(
            [
                f"User task: {task.description}",
                f"Task type: {task.task_type}",
                f"Project path: {task.project_path}",
                f"Tests allowed: {task.allow_tests}",
                f"Graph path: {task.graph_path or 'default'}",
            ]
        ),
        metadata={"static": True},
    )


def output_rules_section() -> ContextSection:
    return ContextSection(
        name="output_rules",
        title="Output Rules",
        source="pycode.agent.prompt_sections.AGENT_SUMMARY_RULES",
        placement=PLACEMENT_SYSTEM,
        content=AGENT_SUMMARY_RULES,
        metadata={"static": True},
    )


def plan_section(steps: list["AgentStep"]) -> ContextSection | None:
    if not steps:
        return None
    lines = ["Planned Agent steps:"]
    for index, step in enumerate(steps, start=1):
        lines.append(
            f"{index}. {step.tool} | required={step.required} | "
            f"todo_id={step.todo_id or 'N/A'} | reason={step.reason or 'N/A'}"
        )
        if step.arguments:
            lines.append(f"   arguments={_format_data(step.arguments)}")
    return ContextSection(
        name="plan",
        title="Plan",
        source="pycode.agent.planner",
        placement=PLACEMENT_USER,
        content="\n".join(lines),
        metadata={"static": False, "steps": len(steps)},
    )


def tool_results_section(
    steps: list["AgentStep"],
    tool_results: list["ToolResult"],
) -> ContextSection | None:
    blocks = []
    for index, (step, result) in enumerate(zip(steps, tool_results), start=1):
        blocks.append(
            "\n".join(
                [
                    f"## Step {index}: {step.tool}",
                    f"Purpose: {step.reason or 'N/A'}",
                    f"Required: {step.required}",
                    f"Status: {'ok' if result.ok else 'failed'}",
                    f"Summary: {result.summary}",
                    f"Error: {result.error or 'N/A'}",
                    "Data:",
                    _format_data(result.data),
                ]
            )
        )
    if not blocks:
        return None
    return ContextSection(
        name="tool_results",
        title="Tool Results",
        source="pycode.tools.ToolResult",
        placement=PLACEMENT_USER,
        content="\n\n".join(
            ["The following evidence came from Agent tool calls:", *blocks]
        ),
        metadata={"static": False, "results": len(tool_results)},
    )


def retrieval_evidence_section(tool_results: list["ToolResult"]) -> ContextSection | None:
    evidence = _extract_evidence(tool_results)
    if not evidence:
        return None
    return ContextSection(
        name="retrieval_evidence",
        title="Retrieval Evidence",
        source="tool_results.data",
        placement=PLACEMENT_USER,
        content="\n".join(f"- {item}" for item in evidence),
        metadata={"static": False, "items": len(evidence)},
    )


def trace_section(trace: "AgentTrace | None") -> ContextSection | None:
    if trace is None:
        return None
    summary = trace.summary()
    tool_lines = [
        (
            f"- turn {tool.turn_index}: {tool.tool} -> {tool.status}; "
            f"summary={tool.summary or 'N/A'}; error={tool.error or 'N/A'}"
        )
        for tool in trace.tools
    ]
    content = "\n".join(
        [
            "Trace summary:",
            f"- run_id: {trace.run_id}",
            f"- status: {trace.status}",
            f"- stop_reason: {trace.stop_reason or 'N/A'}",
            f"- duration_ms: {trace.duration_ms if trace.duration_ms is not None else 'N/A'}",
            (
                "- counts: "
                f"events={summary['events']}, tools={summary['tools']}, "
                f"ok={summary['ok']}, failed={summary['failed']}, denied={summary['denied']}"
            ),
            "Tool timeline:",
            *(tool_lines or ["- N/A"]),
        ]
    )
    return ContextSection(
        name="trace",
        title="Trace",
        source="pycode.agent.trace.AgentTrace",
        placement=PLACEMENT_USER,
        content=content,
        metadata={"static": False, **summary},
    )


def todo_section(todos: list["TodoItem"]) -> ContextSection | None:
    if not todos:
        return None
    counts = {"pending": 0, "in_progress": 0, "completed": 0, "failed": 0}
    for item in todos:
        counts[item.status] = counts.get(item.status, 0) + 1
    lines = [
        "Todo summary:",
        (
            "- counts: "
            f"total={len(todos)}, pending={counts['pending']}, "
            f"in_progress={counts['in_progress']}, completed={counts['completed']}, "
            f"failed={counts['failed']}"
        ),
    ]
    for item in todos:
        lines.append(
            f"- {item.id}: {item.status} | tool={item.tool} | "
            f"title={item.title or item.reason or 'N/A'} | error={item.error or 'N/A'}"
        )
    return ContextSection(
        name="todo",
        title="Todo",
        source="pycode.agent.todo",
        placement=PLACEMENT_USER,
        content="\n".join(lines),
        metadata={"static": False, "total": len(todos), **counts},
    )


def tasks_section(tasks: list["TaskNode"]) -> ContextSection | None:
    if not tasks:
        return None
    lines = ["Task DAG summary:"]
    counts: dict[str, int] = {}
    for task in tasks:
        counts[task.status] = counts.get(task.status, 0) + 1
        blocked_by = ", ".join(task.blocked_by) if task.blocked_by else "N/A"
        lines.append(
            f"- {task.id}: {task.status} | title={task.title} | "
            f"owner={task.owner or 'N/A'} | blocked_by={blocked_by}"
        )
    return ContextSection(
        name="tasks",
        title="Tasks",
        source=".pclens/tasks/*.json",
        placement=PLACEMENT_USER,
        content="\n".join(lines),
        metadata={"static": False, "total": len(tasks), "counts": counts},
    )


def memory_index_section(memory_index: str) -> ContextSection | None:
    if not memory_index:
        return None
    return ContextSection(
        name="memory_index",
        title="Memory Index",
        source=".pclens/memory/MEMORY.md",
        placement=PLACEMENT_SYSTEM,
        content="\n".join(["Project memory index:", memory_index]),
        metadata={"static": False},
    )


def relevant_memories_section(
    relevant_memories: list["MemoryItem"],
) -> ContextSection | None:
    formatted = format_relevant_memories(relevant_memories)
    if not formatted:
        return None
    return ContextSection(
        name="relevant_memories",
        title="Relevant Memories",
        source=".pclens/memory/*.md",
        placement=PLACEMENT_USER,
        content="\n".join(["Relevant project memories:", formatted]),
        metadata={"static": False, "count": len(relevant_memories)},
    )


def _format_data(data: Any) -> str:
    if data in ({}, [], None):
        return "N/A" if data in ({}, None) else "[]"
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _extract_evidence(tool_results: list["ToolResult"]) -> list[str]:
    evidence: list[str] = []
    for result in tool_results:
        data = result.data
        evidence.extend(str(item) for item in data.get("evidence", []))
        evidence.extend(str(item) for item in data.get("files", []))
        if data.get("path"):
            evidence.append(str(data["path"]))
        for item in data.get("matches", []):
            path = item.get("path")
            line_number = item.get("line_number")
            if path and line_number:
                evidence.append(f"{path}:{line_number}")
            elif path:
                evidence.append(str(path))
        for item in data.get("items", []):
            path = item.get("path")
            if path:
                evidence.append(str(path))
            evidence.extend(str(node_id) for node_id in item.get("node_ids", []))
            evidence.extend(str(edge) for edge in item.get("edges", []))
        for edge in data.get("edges", []):
            source = edge.get("source")
            edge_type = edge.get("type")
            target = edge.get("target")
            if source and edge_type and target:
                evidence.append(f"{source} --{edge_type}--> {target}")
        for node in data.get("nodes", []):
            node_id = node.get("id")
            if node_id:
                evidence.append(str(node_id))
    return dedupe_preserve_order(evidence)
