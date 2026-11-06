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
    units: list = field(default_factory=list,repr=False,compare=False)

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
    report: dict | None = None
    discarded_units: list = field(default_factory=list,repr=False)
    verification_warnings: list[str] = field(default_factory=list)
    rejected: bool = False
    pack_digests: list[str] = field(default_factory=list)
    selection_notice: dict = field(default_factory=dict)
    observed_tool_count: int = 0

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
    @staticmethod
    def fit_initial_input(payload,renderer,*,trusted=None):
        from pycode.agent.context_budget import fit_planner
        return fit_planner(payload,renderer,trusted=trusted)

    @staticmethod
    def fit_model_input(context,renderer,*,phase):
        from pycode.agent.context_budget import fit_context
        return fit_context(context,renderer,phase=phase)

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
        turns: list | None = None,
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
        self.turns = turns or []
        self.warnings: list[str] = []

    def assemble(self) -> AgentContext:
        tasks = self.tasks
        if tasks is None and self.load_tasks:
            tasks = self._load_task_dag_summary()

        tool_registry = self.tools if self.tools is not None else self._default_tool_registry()
        candidates = self._build_section_candidates(tasks, tool_registry)
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
            observed_tool_count=len(self.tool_results),
            sections=sorted(sections, key=lambda section: section.priority),
            skipped_sections=sorted(
                skipped_sections, key=lambda section: section.priority
            ),
            warnings=list(self.warnings),
        )
        from pycode.agent.budget import current_budget
        from pycode.agent.context_budget import ContextUnit, encoded, result_units, reference_for, limits
        budget=current_budget()
        trusted=getattr(budget,'retrieval_context',None)
        if trusted is None and self.task.initial_evidence is not None:
            trusted=self.task.initial_evidence.context
        units,discarded,issues,rejected=result_units(self.steps,self.tool_results,self.turns,trusted=trusted)
        context.discarded_units=discarded;context.verification_warnings=issues;context.rejected=rejected
        for section in context.sections:
            if section.name=='project' and trusted is not None and trusted.binding is not None:
                from dataclasses import asdict
                section.content+='\nTrusted server-owned Snapshot binding:\n'+encoded(asdict(trusted.binding))
            elif section.name=='tool_results':
                section.units=units
                section.content='\n\n'.join(u.content for u in units) or 'No selected tool bodies; see context selection decisions.'
            elif section.name=='memory':
                from pycode.agent.memory import format_relevant_memories
                section.units=[ContextUnit('Memory hint; current Snapshot code takes precedence.\n'+format_relevant_memories([item]),
                    {'source':'memory_hint'}) for item in self.relevant_memories]
            elif section.name=='memory_index':
                section.units=[ContextUnit('Memory index hint; not current code evidence.\n'+section.content,
                                           {'source':'memory_hint'})]
            elif section.name=='initial_evidence':
                seed=self.task.initial_evidence
                context.pack_digests.append(seed.digest)
                section.units=[ContextUnit(encoded(dict(origin='question_preflight',initial_pack_digest=seed.digest,
                    item=item.to_dict(),coverage=item.coverage,selection='whole_item_from_original_pack')),
                    reference_for(item.to_dict()),item.source in {'exact_path','exact_symbol'}) for item in seed.result.items]
                keep=[];remaining=limits().tool_output_chars
                for unit in section.units:
                    if len(unit.content)>remaining:context.discarded_units.append((unit,'tool_output_limit'))
                    else:keep.append(unit);remaining-=len(unit.content)
                section.units=keep
        if self.task.initial_evidence is not None and self.task.initial_evidence.digest not in context.pack_digests:
            context.pack_digests.append(self.task.initial_evidence.digest)
        return context

    def _build_section_candidates(
        self,
        tasks: list["TaskNode"] | None,
        tool_registry: dict[str, "ToolSpec"],
    ) -> list[tuple[str, ContextSection | None, str]]:
        from pycode.agent import prompt_sections

        initial = self.task.initial_evidence
        if initial is not None:
            initial.validate(initial.context)
            if any(result.data.get('initial_pack_digest') == initial.digest for result in self.tool_results):
                initial = None
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
        if initial is not None:
            candidates.append(('initial_evidence', prompt_sections.initial_evidence_section(initial),
                               'Snapshot evidence from the question preflight; no tool was executed.'))
        return candidates

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
