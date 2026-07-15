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


class AgentActionType(StrEnum):
    TOOL_CALL = "tool_call"
    FINAL_ANSWER = "final_answer"
    STOP_WITH_ERROR = "stop_with_error"
    NO_OP = "no_op"


@dataclass
class AgentAction:
    type: str
    tool_call: ToolCall | None = None
    reason: str = ""
    answer: str | None = None
    error: str | None = None
    stop_reason: str | None = None

    @classmethod
    def tool(cls, tool_call: ToolCall, reason: str = "") -> "AgentAction":
        return cls(
            type=AgentActionType.TOOL_CALL,
            tool_call=tool_call,
            reason=reason or tool_call.reason,
        )

    @classmethod
    def final(cls, reason: str = "", answer: str | None = None) -> "AgentAction":
        return cls(
            type=AgentActionType.FINAL_ANSWER,
            reason=reason,
            answer=answer,
            stop_reason=AgentStopReason.FINAL,
        )

    @classmethod
    def stop_error(cls, error: str, reason: str = "") -> "AgentAction":
        return cls(
            type=AgentActionType.STOP_WITH_ERROR,
            reason=reason,
            error=error,
            stop_reason=AgentStopReason.ERROR,
        )

    @classmethod
    def no_op(cls, reason: str = "") -> "AgentAction":
        return cls(
            type=AgentActionType.NO_OP,
            reason=reason,
            stop_reason=AgentStopReason.NO_ACTION,
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
    NO_ACTION = "no_action"


@dataclass
class RuntimeConfig:
    max_turns: int = 8
    plan_only: bool = False
    use_llm_planner: bool = True
    enable_trace: bool = True
    enable_memory: bool = True
    enable_memory_extraction: bool = True
    max_relevant_memories: int = 5


@dataclass
class AgentObservation:
    turn_index: int
    action_type: str
    tool_result: ToolResult | None = None
    summary: str = ""
    ok: bool = True
    error: str | None = None


@dataclass
class AgentTurn:
    index: int
    tool_call: ToolCall | None = None
    tool_result: ToolResult | None = None
    action: AgentAction | None = None
    observation: AgentObservation | None = None
    status: str = "observed"


@dataclass
class AgentResult:
    task: AgentTask
    steps: list[AgentStep]
    tool_results: list[ToolResult]
    prompt: str
    answer: str | None = None
    messages: list[AgentMessage] = field(default_factory=list)
    turns: list[AgentTurn] = field(default_factory=list)
    observations: list[AgentObservation] = field(default_factory=list)
    stop_reason: str = AgentStopReason.FINAL
    trace: AgentTrace | None = None
    todos: list[TodoItem] = field(default_factory=list)
    memory: MemoryRunInfo | None = None
    context: AgentContext | None = None
    planner_source: str = "rule"
    planner_error: str | None = None

    @property
    def ok(self) -> bool:
        required_results = [
            result
            for step, result in zip(self.steps, self.tool_results)
            if step.required
        ]
        return all(result.ok for result in required_results)
