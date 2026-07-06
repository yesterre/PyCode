from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pycode.agent.context import AgentContext
    from pycode.agent.memory import MemoryRunInfo
    from pycode.agent.todo import TodoItem
    from pycode.agent.trace import AgentTrace
    from pycode.tools.base import ToolResult


@dataclass
class AgentTask:
    description: str
    project_path: Path
    allow_tests: bool = False
    max_steps: int = 8
    graph_path: Path | None = None
    task_type: str = "general"


@dataclass
class AgentStep:
    tool: str
    arguments: dict = field(default_factory=dict)
    reason: str = ""
    required: bool = True
    todo_id: str | None = None


@dataclass
class ToolCall:
    name: str
    arguments: dict = field(default_factory=dict)
    reason: str = ""
    required: bool = True

    @classmethod
    def from_step(cls, step: AgentStep) -> "ToolCall":
        return cls(
            name=step.tool,
            arguments=dict(step.arguments),
            reason=step.reason,
            required=step.required,
        )


@dataclass
class AgentMessage:
    role: str
    content: str
    tool_call_id: str | None = None
    tool_name: str | None = None


class AgentStopReason(StrEnum):
    FINAL = "final"
    PLAN_ONLY = "plan_only"
    MAX_TURNS = "max_turns"
    ERROR = "error"


@dataclass
class RuntimeConfig:
    max_turns: int = 8
    plan_only: bool = False
    enable_trace: bool = True
    enable_memory: bool = True
    enable_memory_extraction: bool = True
    max_relevant_memories: int = 5


@dataclass
class AgentTurn:
    index: int
    tool_call: ToolCall
    tool_result: ToolResult


@dataclass
class AgentResult:
    task: AgentTask
    steps: list[AgentStep]
    tool_results: list[ToolResult]
    prompt: str
    answer: str | None = None
    messages: list[AgentMessage] = field(default_factory=list)
    turns: list[AgentTurn] = field(default_factory=list)
    stop_reason: str = AgentStopReason.FINAL
    trace: AgentTrace | None = None
    todos: list[TodoItem] = field(default_factory=list)
    memory: MemoryRunInfo | None = None
    context: AgentContext | None = None

    @property
    def ok(self) -> bool:
        required_results = [
            result
            for step, result in zip(self.steps, self.tool_results)
            if step.required
        ]
        return all(result.ok for result in required_results)
