from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from math import isfinite
from pathlib import Path
from typing import Any


def map_agent_trace(trace: Any) -> list[tuple[int, str, str | None, dict[str, Any]]]:
    """Convert a returned Core trace without allowing debug data to break a run."""
    if trace is None:
        return []
    try:
        events = list(trace.events)
    except Exception:
        return []

    mapped = []
    for sequence, event in enumerate(events):
        try:
            raw_data = getattr(event, "data", {})
            safe_data = _json_safe(raw_data)
            if not isinstance(safe_data, dict):
                safe_data = {"value": safe_data}
            raw_tool = safe_data.get("tool")
            tool_name = str(raw_tool)[:255] if raw_tool is not None else None
            payload = {
                "timestamp": _json_safe(getattr(event, "timestamp", None)),
                "message": _json_safe(getattr(event, "message", "")),
                "status": _json_safe(getattr(event, "status", None)),
                "data": safe_data,
            }
            event_type = str(getattr(event, "event_type", "TraceEvent"))[:128] or "TraceEvent"
        except Exception:
            event_type = "TraceEvent"
            tool_name = None
            payload = {"message": "Trace event details were unavailable."}
        mapped.append((sequence, event_type, tool_name, payload))
    return mapped


def _json_safe(value: Any, seen: set[int] | None = None) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if isfinite(value) else str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return _json_safe(value.value, seen)
    if isinstance(value, Path):
        return str(value)

    if seen is None:
        seen = set()
    identity = id(value)
    if identity in seen:
        return "<recursive>"
    seen.add(identity)
    try:
        if isinstance(value, dict):
            return {str(key): _json_safe(item, seen) for key, item in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [_json_safe(item, seen) for item in value]
        try:
            return str(value)
        except Exception:
            return "<unavailable>"
    finally:
        seen.discard(identity)
