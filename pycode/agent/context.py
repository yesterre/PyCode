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

    def __post_init__(self) -> None:
        if self.placement not in PLACEMENTS:
            raise ValueError(f"Unsupported context placement: {self.placement}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "source": self.source,
            "placement": self.placement,
            "content": self.content,
            "metadata": dict(self.metadata),
        }


@dataclass
class AgentContext:
    task: AgentTask
    sections: list[ContextSection] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def section_names(self) -> list[str]:
        return [section.name for section in self.sections]

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
        self.warnings: list[str] = []

    def assemble(self) -> AgentContext:
        from pycode.agent import prompt_sections

        tasks = self.tasks
        if tasks is None and self.load_tasks:
            tasks = self._load_task_dag_summary()

        tool_registry = self.tools or self._default_tool_registry()
        sections = [
            prompt_sections.identity_section(),
            prompt_sections.tools_section(tool_registry),
            prompt_sections.policy_section(),
            prompt_sections.project_section(self.task),
            prompt_sections.output_rules_section(),
            prompt_sections.plan_section(self.steps),
            prompt_sections.tool_results_section(self.steps, self.tool_results),
            prompt_sections.retrieval_evidence_section(self.tool_results),
            prompt_sections.trace_section(self.trace),
            prompt_sections.todo_section(self.todos),
            prompt_sections.tasks_section(tasks or []),
            prompt_sections.memory_index_section(self.memory_index),
            prompt_sections.relevant_memories_section(self.relevant_memories),
        ]
        context = AgentContext(
            task=self.task,
            sections=[section for section in sections if section is not None],
            warnings=list(self.warnings),
        )
        return context

    def _load_task_dag_summary(self) -> list["TaskNode"]:
        try:
            from pycode.agent.task_dag import TaskDAGStore

            return TaskDAGStore(self.task.project_path).list_tasks()
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
