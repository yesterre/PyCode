from __future__ import annotations

from typing import TYPE_CHECKING

from pycode.agent.context import AgentContext, ContextAssembler, ContextSection
from pycode.agent.prompt_sections import AGENT_SUMMARY_RULES

if TYPE_CHECKING:
    from pycode.agent.memory import MemoryItem
    from pycode.agent.task_dag import TaskNode
    from pycode.agent.todo import TodoItem
    from pycode.agent.trace import AgentTrace
    from pycode.agent.types import AgentStep, AgentTask
    from pycode.tools.base import ToolResult, ToolSpec


def build_agent_summary_context(
    task: "AgentTask",
    steps: list["AgentStep"],
    tool_results: list["ToolResult"],
    *,
    memory_index: str = "",
    relevant_memories: list["MemoryItem"] | None = None,
    trace: "AgentTrace | None" = None,
    todos: list["TodoItem"] | None = None,
    tasks: list["TaskNode"] | None = None,
    tools: dict[str, "ToolSpec"] | None = None,
    load_tasks: bool = True,
) -> AgentContext:
    """Build the structured context used by the Agent summary prompt."""
    return ContextAssembler(
        task,
        steps,
        tool_results,
        memory_index=memory_index,
        relevant_memories=relevant_memories,
        trace=trace,
        todos=todos,
        tasks=tasks,
        tools=tools,
        load_tasks=load_tasks,
    ).assemble()


def build_agent_summary_prompt(
    task: "AgentTask",
    steps: list["AgentStep"],
    tool_results: list["ToolResult"],
    *,
    memory_index: str = "",
    relevant_memories: list["MemoryItem"] | None = None,
    trace: "AgentTrace | None" = None,
    todos: list["TodoItem"] | None = None,
    tasks: list["TaskNode"] | None = None,
    tools: dict[str, "ToolSpec"] | None = None,
) -> str:
    """Build a stable prompt for summarizing Agent runtime evidence."""
    context = build_agent_summary_context(
        task,
        steps,
        tool_results,
        memory_index=memory_index,
        relevant_memories=relevant_memories,
        trace=trace,
        todos=todos,
        tasks=tasks,
        tools=tools,
    )
    return render_agent_prompt(context)


def render_agent_prompt(context: AgentContext) -> str:
    static_sections = [
        section
        for section in context.sections
        if section.metadata.get("static") is True
    ]
    dynamic_sections = [
        section
        for section in context.sections
        if section.metadata.get("static") is not True
    ]
    parts = ["--- Static Context ---"]
    parts.extend(_render_section(section) for section in static_sections)
    parts.append("--- Dynamic Context ---")
    parts.extend(_render_section(section) for section in dynamic_sections)
    if "tool_results" not in context.section_names():
        parts.extend(
            [
                "## Tool Results",
                "placement: user",
                "source: pycode.tools.ToolResult",
                "",
                "The following evidence came from Agent tool calls:",
                "No tools were executed.",
            ]
        )
    if context.warnings:
        parts.extend(
            [
                "## Context Warnings",
                "placement: user",
                "source: pycode.agent.context",
                "",
                "\n".join(f"- {warning}" for warning in context.warnings),
            ]
        )
    return "\n\n".join(part for part in parts if part)


def _render_section(section: ContextSection) -> str:
    return "\n".join(
        [
            f"## {section.title}",
            f"placement: {section.placement}",
            f"source: {section.source}",
            "",
            section.content,
        ]
    )
