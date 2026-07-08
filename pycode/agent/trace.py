from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from time import perf_counter
from typing import Any
from uuid import uuid4

from pycode.agent._time_utils import format_timestamp, utc_now
from pycode.agent.types import AgentTask, ToolCall
from pycode.tools import ToolResult


MAX_SUMMARY_CHARS = 500
MAX_DATA_VALUE_CHARS = 200
MAX_DATA_ITEMS = 8


@dataclass
class TraceEvent:
    event_type: str
    timestamp: datetime
    message: str = ""
    status: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "timestamp": format_timestamp(self.timestamp),
            "message": self.message,
            "status": self.status,
            "data": self.data,
        }


@dataclass
class ToolTrace:
    turn_index: int
    tool: str
    started_at: datetime
    arguments: dict[str, Any] = field(default_factory=dict)
    ended_at: datetime | None = None
    duration_ms: float | None = None
    status: str = "running"
    summary: str = ""
    error: str | None = None
    denied_by: str | None = None
    result_data: dict[str, Any] = field(default_factory=dict)
    _started_counter: float = field(default_factory=perf_counter, repr=False)

    def finish(self, result: ToolResult) -> None:
        self.ended_at = utc_now()
        self.duration_ms = round((perf_counter() - self._started_counter) * 1000, 3)
        self.summary = truncate_string(result.summary, MAX_SUMMARY_CHARS)
        self.error = truncate_string(result.error, MAX_SUMMARY_CHARS) if result.error else None
        self.result_data = summarize_mapping(result.data)
        self.denied_by = result.data.get("denied_by") if result.data.get("denied") else None
        if self.denied_by:
            self.status = "denied"
        elif result.ok:
            self.status = "ok"
        else:
            self.status = "failed"

    def deny(self, denied_by: str, summary: str, error: str | None = None) -> None:
        self.ended_at = utc_now()
        self.duration_ms = round((perf_counter() - self._started_counter) * 1000, 3)
        self.status = "denied"
        self.denied_by = denied_by
        self.summary = truncate_string(summary, MAX_SUMMARY_CHARS)
        self.error = truncate_string(error, MAX_SUMMARY_CHARS) if error else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_index": self.turn_index,
            "tool": self.tool,
            "started_at": format_timestamp(self.started_at),
            "ended_at": format_timestamp(self.ended_at) if self.ended_at else None,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "arguments": self.arguments,
            "summary": self.summary,
            "error": self.error,
            "denied_by": self.denied_by,
            "result_data": self.result_data,
        }


@dataclass
class AgentTrace:
    run_id: str
    task_description: str
    project_path: str
    started_at: datetime
    ended_at: datetime | None = None
    duration_ms: float | None = None
    status: str = "running"
    stop_reason: str | None = None
    events: list[TraceEvent] = field(default_factory=list)
    tools: list[ToolTrace] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_description": self.task_description,
            "project_path": self.project_path,
            "started_at": format_timestamp(self.started_at),
            "ended_at": format_timestamp(self.ended_at) if self.ended_at else None,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "events": [event.to_dict() for event in self.events],
            "tools": [tool.to_dict() for tool in self.tools],
            "summary": self.summary(),
        }

    def summary(self) -> dict[str, int]:
        ok = sum(1 for tool in self.tools if tool.status == "ok")
        failed = sum(1 for tool in self.tools if tool.status == "failed")
        denied = sum(1 for tool in self.tools if tool.status == "denied")
        return {
            "events": len(self.events),
            "tools": len(self.tools),
            "ok": ok,
            "failed": failed,
            "denied": denied,
        }


class TraceRecorder:
    def __init__(self, task: AgentTask) -> None:
        self._started_counter = perf_counter()
        self.trace = AgentTrace(
            run_id=str(uuid4()),
            task_description=task.description,
            project_path=str(task.project_path),
            started_at=utc_now(),
        )

    def record_event(
        self,
        event_type: str,
        message: str = "",
        *,
        status: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> TraceEvent:
        event = TraceEvent(
            event_type=event_type,
            timestamp=utc_now(),
            message=truncate_string(message, MAX_SUMMARY_CHARS),
            status=status,
            data=summarize_mapping(data or {}),
        )
        self.trace.events.append(event)
        return event

    def start_tool(self, turn_index: int, tool_call: ToolCall) -> ToolTrace:
        tool_trace = ToolTrace(
            turn_index=turn_index,
            tool=tool_call.name,
            started_at=utc_now(),
            arguments=summarize_mapping(tool_call.arguments),
        )
        self.trace.tools.append(tool_trace)
        return tool_trace

    def finish_tool(self, turn_index: int, result: ToolResult) -> ToolTrace | None:
        tool_trace = self._find_tool(turn_index, result.tool)
        if tool_trace is None:
            return None
        tool_trace.finish(result)
        return tool_trace

    def deny_tool(
        self,
        turn_index: int,
        tool_name: str,
        denied_by: str,
        summary: str,
        error: str | None = None,
    ) -> ToolTrace | None:
        tool_trace = self._find_tool(turn_index, tool_name)
        if tool_trace is None:
            return None
        tool_trace.deny(denied_by, summary, error)
        return tool_trace

    def finish(self, stop_reason: str, *, status: str = "finished") -> AgentTrace:
        self.trace.ended_at = utc_now()
        self.trace.duration_ms = round((perf_counter() - self._started_counter) * 1000, 3)
        self.trace.status = status
        self.trace.stop_reason = stop_reason
        return self.trace

    def _find_tool(self, turn_index: int, tool_name: str) -> ToolTrace | None:
        for tool_trace in reversed(self.trace.tools):
            if tool_trace.turn_index == turn_index and tool_trace.tool == tool_name:
                return tool_trace
        return None


def summarize_mapping(data: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for index, (key, value) in enumerate(data.items()):
        if index >= MAX_DATA_ITEMS:
            summary["..."] = f"{len(data) - MAX_DATA_ITEMS} more keys"
            break
        summary[str(key)] = summarize_value(value)
    return summary


def summarize_value(value: Any) -> Any:
    if isinstance(value, str):
        return truncate_string(value, MAX_DATA_VALUE_CHARS)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return summarize_mapping(value)
    if isinstance(value, (list, tuple)):
        items = [summarize_value(item) for item in value[:MAX_DATA_ITEMS]]
        if len(value) > MAX_DATA_ITEMS:
            items.append(f"... {len(value) - MAX_DATA_ITEMS} more items")
        return items
    return truncate_string(str(value), MAX_DATA_VALUE_CHARS)


def truncate_string(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + "...[truncated]"
