from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable

from pycode.agent.trace import TraceRecorder
from pycode.agent.types import AgentTask, ToolCall
from pycode.tools import ToolResult


class HookEventType(StrEnum):
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    STOP = "Stop"


@dataclass
class HookResult:
    allowed: bool = True
    message: str = ""
    denied_by: str | None = None
    data: dict = field(default_factory=dict)

    @classmethod
    def allow(cls, message: str = "", **data) -> "HookResult":
        return cls(allowed=True, message=message, data=data)

    @classmethod
    def deny(
        cls,
        message: str,
        *,
        denied_by: str = "hook",
        **data,
    ) -> "HookResult":
        return cls(
            allowed=False,
            message=message,
            denied_by=denied_by,
            data=data,
        )


@dataclass
class HookContext:
    event_type: str
    task: AgentTask
    trace_recorder: TraceRecorder | None = None
    tool_call: ToolCall | None = None
    tool_result: ToolResult | None = None
    turn_index: int | None = None
    stop_reason: str | None = None
    data: dict = field(default_factory=dict)


HookHandler = Callable[[HookContext], HookResult | None]


class HookRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, list[HookHandler]] = defaultdict(list)

    def register(self, event_type: str, handler: HookHandler) -> None:
        self._handlers[event_type].append(handler)

    def extend(self, other: "HookRegistry") -> None:
        for event_type, handlers in other._handlers.items():
            self._handlers[event_type].extend(handlers)

    def trigger(self, context: HookContext) -> list[HookResult]:
        results: list[HookResult] = []
        for handler in self._handlers.get(context.event_type, []):
            try:
                result = handler(context)
                results.append(result or HookResult.allow())
            except Exception as exc:  # pragma: no cover - defensive boundary
                if context.trace_recorder is not None:
                    context.trace_recorder.record_event(
                        "HookError",
                        f"Hook handler failed for {context.event_type}.",
                        status="error",
                        data={
                            "handler": getattr(handler, "__name__", repr(handler)),
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                    )
                results.append(
                    HookResult.allow(
                        "Hook handler failed and was ignored.",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
        return results


def create_default_hook_registry() -> HookRegistry:
    registry = HookRegistry()
    registry.register(HookEventType.USER_PROMPT_SUBMIT, record_trace_hook)
    registry.register(HookEventType.PRE_TOOL_USE, record_trace_hook)
    registry.register(HookEventType.POST_TOOL_USE, record_trace_hook)
    registry.register(HookEventType.STOP, record_trace_hook)
    return registry


def record_trace_hook(context: HookContext) -> HookResult:
    recorder = context.trace_recorder
    if recorder is None:
        return HookResult.allow()

    if context.event_type == HookEventType.USER_PROMPT_SUBMIT:
        recorder.record_event(
            context.event_type,
            "User task submitted.",
            status="ok",
            data={"task": context.task.description},
        )
    elif context.event_type == HookEventType.PRE_TOOL_USE:
        if context.tool_call is not None and context.turn_index is not None:
            recorder.record_event(
                context.event_type,
                f"Preparing tool {context.tool_call.name}.",
                status="running",
                data={
                    "turn_index": context.turn_index,
                    "tool": context.tool_call.name,
                },
            )
            recorder.start_tool(context.turn_index, context.tool_call)
    elif context.event_type == HookEventType.POST_TOOL_USE:
        if (
            context.tool_result is not None
            and context.turn_index is not None
        ):
            recorder.finish_tool(context.turn_index, context.tool_result)
            recorder.record_event(
                context.event_type,
                f"Tool {context.tool_result.tool} finished.",
                status="ok" if context.tool_result.ok else "failed",
                data={
                    "turn_index": context.turn_index,
                    "tool": context.tool_result.tool,
                    "summary": context.tool_result.summary,
                    "error": context.tool_result.error,
                    "denied": context.tool_result.data.get("denied", False),
                    "denied_by": context.tool_result.data.get("denied_by"),
                },
            )
    elif context.event_type == HookEventType.STOP:
        recorder.record_event(
            context.event_type,
            "Agent runtime stopped.",
            status="ok",
            data={"stop_reason": context.stop_reason},
        )
    return HookResult.allow()
