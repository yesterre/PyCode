from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pycode.agent.types import AgentStep, AgentTask

if TYPE_CHECKING:
    from pycode.agent.memory import MemoryItem
    from pycode.agent.task_dag import TaskNode
    from pycode.agent.todo import TodoItem
    from pycode.agent.trace import AgentTrace
    from pycode.tools.base import ToolResult, ToolSpec


PLACEMENT_SYSTEM = "system"
PLACEMENT_USER = "user"
PLACEMENTS = {PLACEMENT_SYSTEM, PLACEMENT_USER}


@dataclass
class ContextSection:
    name: str
    title: str
    source: str
    placement: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    priority: int = 100
    size_estimate: int = 0
    included: bool = True
    reason: str = ""

    def __post_init__(self) -> None:
        if self.placement not in PLACEMENTS:
            raise ValueError(f"Unsupported context placement: {self.placement}")
        if self.size_estimate <= 0:
            self.size_estimate = len(self.content)
        if not self.reason:
            self.reason = "included" if self.included else "skipped"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "source": self.source,
            "placement": self.placement,
            "content": self.content,
            "metadata": dict(self.metadata),
            "priority": self.priority,
            "size_estimate": self.size_estimate,
            "included": self.included,
            "reason": self.reason,
        }


@dataclass
class AgentContext:
    task: AgentTask
    sections: list[ContextSection] = field(default_factory=list)
    skipped_sections: list[ContextSection] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def section_names(self) -> list[str]:
        return [section.name for section in self.sections]

    def all_sections(self) -> list[ContextSection]:
        return [*self.sections, *self.skipped_sections]

    def debug_dict(self) -> dict[str, Any]:
        return {
            "included": [
                _section_debug(section)
                for section in sorted(self.sections, key=lambda item: item.priority)
            ],
            "skipped": [
                _section_debug(section)
                for section in sorted(self.skipped_sections, key=lambda item: item.priority)
            ],
            "warnings": list(self.warnings),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": {
                "description": self.task.description,
                "project_path": str(self.task.project_path),
                "allow_tests": self.task.allow_tests,
                "max_steps": self.task.max_steps,
                "graph_path": str(self.task.graph_path) if self.task.graph_path else None,
                "task_type": self.task.task_type,
            },
            "sections": [section.to_dict() for section in self.sections],
            "skipped_sections": [
                section.to_dict() for section in self.skipped_sections
            ],
            "warnings": list(self.warnings),
        }

    def render_key(self) -> str:
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )


class ContextAssembler:
    def __init__(
        self,
        task: AgentTask,
        steps: list[AgentStep],
        tool_results: list["ToolResult"],
        *,
        memory_index: str = "",
        relevant_memories: list["MemoryItem"] | None = None,
        trace: "AgentTrace | None" = None,
        todos: list["TodoItem"] | None = None,
        tasks: list["TaskNode"] | None = None,
        tools: dict[str, "ToolSpec"] | None = None,
        load_tasks: bool = True,
        turn_index: int | None = None,
    ) -> None:
        self.task = task
        self.steps = steps
        self.tool_results = tool_results
        self.memory_index = memory_index
        self.relevant_memories = relevant_memories or []
        self.trace = trace
        self.todos = todos or []
        self.tasks = tasks
        self.tools = tools
        self.load_tasks = load_tasks
        self.turn_index = turn_index
        self.warnings: list[str] = []

    def assemble(self) -> AgentContext:
        from pycode.agent import prompt_sections

        tasks = self.tasks
        if tasks is None and self.load_tasks:
            tasks = self._load_task_dag_summary()

        tool_registry = self.tools or self._default_tool_registry()
        candidates = [
            (
                "identity",
                prompt_sections.identity_section(),
                "Agent identity is always included.",
            ),
            (
                "tools",
                prompt_sections.tools_section(tool_registry),
                "Tool registry is always included.",
            ),
            (
                "policy",
                prompt_sections.policy_section(),
                "Policy rules are always included.",
            ),
            (
                "project",
                prompt_sections.project_section(self.task),
                "Project and user task are always included.",
            ),
            (
                "response_contract",
                prompt_sections.response_contract_section(),
                "Response contract is always included.",
            ),
            (
                "plan",
                prompt_sections.plan_section(self.steps),
                "Initial plan is included when planned steps exist.",
            ),
            (
                "tool_results",
                prompt_sections.tool_results_section(self.steps, self.tool_results),
                "Tool results are included after tools have run.",
            ),
            (
                "evidence",
                prompt_sections.evidence_section(self.tool_results),
                "Evidence is included when tool results expose evidence refs.",
            ),
            (
                "trace",
                prompt_sections.trace_section(self.trace),
                "Trace is included when tracing is enabled.",
            ),
            (
                "todo",
                prompt_sections.todo_section(self.todos),
                "Todo state is included when todos exist.",
            ),
            (
                "tasks",
                prompt_sections.tasks_section(tasks or []),
                "Task DAG state is included when tasks exist.",
            ),
            (
                "memory_index",
                prompt_sections.memory_index_section(self.memory_index),
                "Memory index is included when it exists.",
            ),
            (
                "memory",
                prompt_sections.relevant_memories_section(self.relevant_memories),
                "Relevant memory bodies are included when selected.",
            ),
        ]
        sections: list[ContextSection] = []
        skipped_sections: list[ContextSection] = []
        for name, section, reason in candidates:
            if section is None:
                skipped_sections.append(
                    skipped_section(
                        name,
                        source="pycode.agent.context.ContextAssembler",
                        reason=f"Skipped because no data was available. {reason}",
                        turn_index=self.turn_index,
                    )
                )
                continue
            section.reason = reason
            if self.turn_index is not None:
                section.metadata.setdefault("turn_index", self.turn_index)
            sections.append(section)
        context = AgentContext(
            task=self.task,
            sections=sorted(sections, key=lambda section: section.priority),
            skipped_sections=sorted(
                skipped_sections, key=lambda section: section.priority
            ),
            warnings=list(self.warnings),
        )
        return context

    def _load_task_dag_summary(self) -> list["TaskNode"]:
        try:
            from pycode.agent.task_dag import TaskDAGStore

            store = TaskDAGStore(self.task.project_path)
            tasks = store.list_tasks()
            for task in tasks:
                can_start = store.can_start(task)
                task.metadata["can_start"] = can_start.can_start
                task.metadata["active_blocks"] = list(can_start.blocked_by)
                task.metadata["missing_dependencies"] = list(
                    can_start.missing_dependencies
                )
            return tasks
        except (OSError, PermissionError, ValueError) as exc:
            self.warnings.append(f"Task DAG context unavailable: {type(exc).__name__}: {exc}")
            return []

    @staticmethod
    def _default_tool_registry() -> dict[str, "ToolSpec"]:
        try:
            from pycode.tools import TOOLS

            return TOOLS
        except Exception:
            return {}


def relative_task_path(path: str | Path) -> str:
    return Path(path).as_posix()


def skipped_section(
    name: str,
    *,
    source: str,
    reason: str,
    turn_index: int | None = None,
) -> ContextSection:
    metadata: dict[str, Any] = {"static": False}
    if turn_index is not None:
        metadata["turn_index"] = turn_index
    return ContextSection(
        name=name,
        title=name.replace("_", " ").title(),
        source=source,
        placement=PLACEMENT_USER,
        content="",
        metadata=metadata,
        priority=999,
        size_estimate=0,
        included=False,
        reason=reason,
    )


def _section_debug(section: ContextSection) -> dict[str, Any]:
    return {
        "name": section.name,
        "title": section.title,
        "source": section.source,
        "placement": section.placement,
        "priority": section.priority,
        "included": section.included,
        "reason": section.reason,
        "size_estimate": section.size_estimate,
        "metadata": dict(section.metadata),
    }
