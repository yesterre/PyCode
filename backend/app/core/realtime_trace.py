"""Per-attempt, best-effort live projection of public Application Trace events."""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from backend.app.core.application_trace import (
    ApplicationEventType, ApplicationTraceEvent, ApplicationTraceFailure,
    ApplicationTraceNormalizer, FailureStage, _aware, _duration_ms,
    _llm_observation_counts, _safe_collection, normalize_error, sanitize_text,
)


logger = logging.getLogger("pycode.backend.realtime_trace")


class RealtimeTraceSession:
    def __init__(self, run: Any, ownership: Any, publisher: Any = None) -> None:
        self.run_id = run.id
        self.started_at = run.started_at
        self.ownership = ownership
        self.publisher = publisher
        self.events: list[ApplicationTraceEvent] = []
        self.degraded = False
        self.observed = False
        self._reasons: dict[tuple[Any, str], str] = {}
        self._plan_emitted = False
        self._open_llm: dict[tuple[str, Any], int] = {}
        self._ownership_lost = False
        self._append(ApplicationEventType.RUN_STARTED, run.started_at, None, {
            "run_type": str(run.run_type), "snapshot_id": str(run.snapshot_id),
        }, publish=True)

    def _append(
        self, event_type: ApplicationEventType, occurred_at: Any,
        tool_name: str | None, payload: dict[str, Any], *, publish: bool,
    ) -> ApplicationTraceEvent:
        event = ApplicationTraceEvent(
            len(self.events), event_type, occurred_at, tool_name, payload,
        )
        self.events.append(event)
        if publish:
            self.publish(event)
        return event

    def publish(self, event: ApplicationTraceEvent) -> None:
        if self._ownership_lost:
            return
        try:
            if event.event_type not in {
                ApplicationEventType.RUN_COMPLETED, ApplicationEventType.RUN_FAILED,
            }:
                self.ownership.ensure_owned()
        except Exception:
            self._ownership_lost = True
            logger.warning("Realtime Trace ownership lost run_id=%s", self.run_id, exc_info=True)
            return
        try:
            if self.publisher is not None:
                self.publisher.publish(self.run_id, event)
        except Exception:
            logger.warning("Realtime Trace publish skipped run_id=%s", self.run_id, exc_info=True)

    def observe(self, kind: str, value: Any) -> None:
        self.observed = True
        try:
            normalizer = ApplicationTraceNormalizer
            if kind == "reason":
                data = value.data
                if data.get("tool"):
                    self._reasons[(data.get("turn_index"), str(data["tool"]))] = str(data.get("reason") or "")
                return
            if kind == "plan":
                if self._plan_emitted:
                    return
                event_type, steps, occurred_at = value
                payload = normalizer.plan_payload_from_steps(event_type, steps)
                self._append(ApplicationEventType.PLAN_CREATED, occurred_at, None, payload, publish=True)
                self._plan_emitted = True
                return
            if kind == "context":
                data = value.data
                if not isinstance(data.get("included_section_names"), list) or not isinstance(data.get("skipped_section_names"), list):
                    raise ValueError("Missing context identifiers")
                self._append(ApplicationEventType.CONTEXT_UPDATED, value.timestamp, None,
                             normalizer._context_payload(data), publish=True)
                return
            if kind in {"llm_started", "llm_finished"}:
                data = value.data
                purpose = str(data.get("purpose", ""))
                key = (purpose, data.get("turn_index"))
                payload: dict[str, Any] = {"purpose": purpose}
                if isinstance(data.get("turn_index"), int):
                    payload["turn_index"] = data["turn_index"]
                if kind == "llm_started":
                    self._append(ApplicationEventType.LLM_STARTED, value.timestamp, None, payload, publish=True)
                    self._open_llm[key] = self._open_llm.get(key, 0) + 1
                else:
                    success = bool(data.get("success"))
                    payload.update(success=success, fallback_used=bool(data.get("fallback_used", False)))
                    if not success:
                        payload["error"] = normalize_error(
                            data.get("error_category"), code=data.get("error_code"),
                        ).to_dict()
                    self._append(ApplicationEventType.LLM_FINISHED, value.timestamp, None, payload, publish=True)
                    if self._open_llm.get(key, 0):
                        self._open_llm[key] -= 1
                    else:
                        self.degraded = True
                return
            if kind == "tool_started":
                key = (getattr(value, "turn_index", None), str(getattr(value, "tool", "")))
                draft = normalizer._tool_started(value, self._reasons.get(key))
                self._append(*draft, publish=True)
                return
            if kind == "tool_finished":
                self._append(*normalizer._tool_finished(value), publish=True)
        except Exception:
            self.degraded = True
            logger.warning("Realtime Trace projection skipped run_id=%s kind=%s", self.run_id, kind, exc_info=True)

    def append_terminal(
        self, run: Any, *, result: Any = None,
        failure: ApplicationTraceFailure | None = None,
    ) -> ApplicationTraceEvent:
        trace = getattr(result, "trace", None) if result is not None else None
        internals, events_available = _safe_collection(trace, "events")
        tools, tools_available = _safe_collection(trace, "tools")
        starts, finishes, expected, counts_valid = _llm_observation_counts(internals)
        public_starts = Counter(
            event.payload["purpose"] for event in self.events
            if event.event_type == ApplicationEventType.LLM_STARTED
        )
        public_finishes = Counter(
            event.payload["purpose"] for event in self.events
            if event.event_type == ApplicationEventType.LLM_FINISHED
        )
        public_counts_match = starts == public_starts and finishes == public_finishes
        plan_expected = any(str(getattr(event, "event_type", "")) in {
            "LLMPlanGenerated", "LLMPlanFallback", "PlannerSelected",
        } for event in internals)
        context_expected = sum(
            str(getattr(event, "event_type", "")) == "ContextAssembled"
            for event in internals
        )
        public_counts_match &= (
            sum(event.event_type == ApplicationEventType.PLAN_CREATED for event in self.events)
            == int(plan_expected)
            and sum(event.event_type == ApplicationEventType.CONTEXT_UPDATED for event in self.events)
            == context_expected
            and sum(event.event_type == ApplicationEventType.TOOL_STARTED for event in self.events)
            == len(tools)
            and sum(event.event_type == ApplicationEventType.TOOL_FINISHED for event in self.events)
            == sum(getattr(tool, "ended_at", None) is not None for tool in tools)
        )
        key_events_valid = all(
            _aware(getattr(event, "timestamp", None)) and isinstance(getattr(event, "data", None), dict)
            for event in internals
            if str(getattr(event, "event_type", "")) in {
                "LLMPlanGenerated", "LLMPlanFallback", "PlannerSelected",
                "ContextAssembled", "LLMCallStarted", "LLMCallFinished",
                "PreToolUse", "PostToolUse",
            }
        )
        complete = bool(
            result is not None and trace is not None and events_available
            and tools_available and counts_valid and key_events_valid
            and public_counts_match and not self.degraded
            and self.events and self.events[0].event_type == ApplicationEventType.RUN_STARTED
            and _aware(run.finished_at)
            and not any(self._open_llm.values())
            and all(starts[p] >= n and finishes[p] >= n for p, n in expected.items())
        )
        duration = _duration_ms(self.started_at, run.finished_at)
        if str(run.status) == "completed":
            payload: dict[str, Any] = {
                "stop_reason": sanitize_text(
                    getattr(result, "stop_reason", None)
                    or getattr(trace, "stop_reason", None) or "final"
                ),
                "has_result": run.result is not None,
                "trace_complete": complete,
            }
            event_type = ApplicationEventType.RUN_COMPLETED
        else:
            resolved = failure or ApplicationTraceFailure(
                stage=FailureStage.AGENT_CORE,
                error=normalize_error("agent_error", code="agent_run_failed"),
            )
            payload = {"stage": resolved.stage.value, "error": resolved.error.to_dict(),
                       "trace_complete": complete}
            event_type = ApplicationEventType.RUN_FAILED
        if duration is not None:
            payload["duration_ms"] = duration
        return self._append(event_type, run.finished_at, None, payload, publish=False)
