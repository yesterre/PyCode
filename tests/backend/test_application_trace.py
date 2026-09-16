from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.core.application_trace import (
    APPLICATION_TRACE_SCHEMA_VERSION,
    ApplicationEventType,
    ApplicationTraceEvent,
    ApplicationTraceNormalizer,
    PublicErrorCategory,
    application_event_from_record,
    sanitize_arguments,
)
from backend.app.core.models import AgentRun, AgentRunStatus, TraceEvent
from pycode.agent.trace import AgentTrace, ToolTrace, TraceEvent as InternalTraceEvent
from pycode.agent.types import AgentResult, AgentStep, AgentStopReason, AgentTask


NOW = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)


def _run(status=AgentRunStatus.COMPLETED, result="answer"):
    return AgentRun(
        id=uuid4(), project_id=uuid4(), snapshot_id=uuid4(), task="audit",
        run_type="agent", status=status, model=None, prompt_tokens=None,
        completion_tokens=None, result=result if status == "completed" else None,
        error_message=None if status == "completed" else "safe failure",
        started_at=NOW, finished_at=NOW + timedelta(seconds=2), created_at=NOW,
        core_started_at=NOW + timedelta(milliseconds=10),
    )


def test_application_event_contract_enforces_envelope_and_payload():
    event = ApplicationTraceEvent(
        0, ApplicationEventType.RUN_STARTED, NOW, None,
        {"run_type": "agent", "snapshot_id": str(uuid4())},
    )
    assert event.schema_version == APPLICATION_TRACE_SCHEMA_VERSION
    assert event.database_payload()["occurred_at"].endswith("Z")

    with pytest.raises(ValueError, match="tool_name"):
        ApplicationTraceEvent(
            0, ApplicationEventType.TOOL_STARTED, NOW, None, {},
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        ApplicationTraceEvent(
            0, ApplicationEventType.RUN_STARTED, NOW.replace(tzinfo=None), None,
            {"run_type": "agent", "snapshot_id": str(uuid4())},
        )
    with pytest.raises(ValueError, match="requires an error"):
        ApplicationTraceEvent(
            0, ApplicationEventType.LLM_FINISHED, NOW, None,
            {"purpose": "plan", "success": False, "fallback_used": True},
        )
    with pytest.raises(ValueError, match="size limit"):
        ApplicationTraceEvent(
            0, ApplicationEventType.TOOL_STARTED, NOW, "query_graph",
            {"arguments": {"value": "x" * 20_000}},
        )


def test_sensitive_arguments_are_recursive_bounded_and_redacted():
    sanitized = sanitize_arguments({
        "authorization": "Bearer private",
        "nested": {"api-key": "secret", "safe": "ok"},
        "items": list(range(100)),
    })
    assert sanitized is not None
    assert sanitized["authorization"] == "[REDACTED]"
    assert sanitized["nested"]["api-key"] == "[REDACTED]"
    assert sanitized["nested"]["safe"] == "ok"
    assert len(sanitized["items"]) == 20


def test_normalizer_projects_internal_trace_with_continuous_sequence():
    llm_events = []
    purposes = ["plan", "next_action", "summary", "memory_selection", "memory_extraction"]
    for index, purpose in enumerate(purposes):
        llm_events.extend([
            InternalTraceEvent(
                "LLMCallStarted", NOW + timedelta(milliseconds=10 + index * 2),
                data={"purpose": purpose, "turn_index": 1 if purpose == "next_action" else None},
            ),
            InternalTraceEvent(
                "LLMCallFinished", NOW + timedelta(milliseconds=11 + index * 2),
                data={"purpose": purpose, "success": True, "fallback_used": False,
                      "turn_index": 1 if purpose == "next_action" else None},
            ),
        ])
    internal = [
        llm_events[0], llm_events[1],
        InternalTraceEvent("LLMPlanGenerated", NOW + timedelta(milliseconds=20)),
        InternalTraceEvent(
            "ContextAssembled", NOW + timedelta(milliseconds=30),
            data={
                "turn_index": 1,
                "included_section_names": [
                    "identity", "tools", "policy", "project", "plan", "unknown",
                ],
                "skipped_section_names": ["memory", "memory_index"],
                "warnings": ["safe warning"],
            },
        ),
        *llm_events[2:],
        InternalTraceEvent(
            "NextActionDecided", NOW + timedelta(milliseconds=50),
            data={"turn_index": 1, "tool": "query_graph", "reason": "inspect graph"},
        ),
        InternalTraceEvent(
            "PreToolUse", NOW + timedelta(milliseconds=60),
            data={"turn_index": 1, "tool": "query_graph"},
        ),
        InternalTraceEvent("UnknownInternal", NOW + timedelta(milliseconds=65)),
        InternalTraceEvent(
            "PostToolUse", NOW + timedelta(milliseconds=70),
            data={"turn_index": 1, "tool": "query_graph"},
        ),
    ]
    tool = ToolTrace(
        turn_index=1, tool="query_graph", started_at=NOW + timedelta(milliseconds=61),
        ended_at=NOW + timedelta(milliseconds=69), duration_ms=8.0, status="ok",
        arguments={"api_key": "private", "query": "entry"}, summary="found graph",
    )
    trace = AgentTrace(
        "internal", "audit", ".", NOW, ended_at=NOW + timedelta(seconds=1),
        status="finished", stop_reason="final", events=internal, tools=[tool],
    )
    result = AgentResult(
        AgentTask("audit", __import__("pathlib").Path(".")),
        [AgentStep("query_graph", reason="inspect graph", required=False)], [], "prompt",
        answer="answer", stop_reason=AgentStopReason.FINAL, trace=trace,
    )

    normalized = ApplicationTraceNormalizer().normalize(_run(), result=result)
    assert normalized.trace_complete is True
    assert [event.sequence for event in normalized.events] == list(range(len(normalized.events)))
    assert normalized.events[0].event_type == ApplicationEventType.RUN_STARTED
    assert normalized.events[-1].event_type == ApplicationEventType.RUN_COMPLETED
    assert normalized.events[-1].payload["trace_complete"] is True
    assert sum(event.event_type == "tool_started" for event in normalized.events) == 1
    assert sum(event.event_type == "tool_finished" for event in normalized.events) == 1
    started = next(event for event in normalized.events if event.event_type == "tool_started")
    assert started.occurred_at == tool.started_at
    assert started.payload["arguments"]["api_key"] == "[REDACTED]"
    context = next(event for event in normalized.events if event.event_type == "context_updated")
    assert context.payload["included_sections"] == ["system", "project", "plan"]
    assert context.payload["skipped_sections"] == ["memory"]
    assert "unknown" not in str(context.payload)
    for purpose in purposes:
        assert sum(
            event.event_type == "llm_started" and event.payload["purpose"] == purpose
            for event in normalized.events
        ) == 1
        assert sum(
            event.event_type == "llm_finished" and event.payload["purpose"] == purpose
            for event in normalized.events
        ) == 1


@pytest.mark.parametrize(
    ("included_internal", "skipped_internal", "expected_included", "expected_skipped"),
    [
        (["tasks"], ["todo"], ["task_state"], []),
        (["todo"], ["tasks"], ["task_state"], []),
        ([], ["tasks", "todo"], [], ["task_state"]),
        (["tasks", "todo"], [], ["task_state"], []),
        (
            ["memory", "identity", "evidence", "project"],
            ["trace", "tool_results", "plan"],
            ["system", "project", "evidence", "memory"],
            ["plan", "tool_results", "execution_history"],
        ),
        (["unknown_included", "project"], ["unknown_skipped", "memory"],
         ["project"], ["memory"]),
    ],
)
def test_context_projection_included_wins_and_sections_are_disjoint(
    included_internal,
    skipped_internal,
    expected_included,
    expected_skipped,
):
    payload = ApplicationTraceNormalizer._context_payload({
        "included_section_names": included_internal,
        "skipped_section_names": skipped_internal,
    })

    included = payload["included_sections"]
    skipped = payload.get("skipped_sections", [])
    assert included == expected_included
    assert skipped == expected_skipped
    assert set(included).isdisjoint(set(skipped))
    assert "unknown_included" not in included
    assert "unknown_skipped" not in skipped


def test_legacy_and_malformed_rows_are_not_application_events():
    run_id = uuid4()
    legacy = TraceEvent(uuid4(), run_id, 0, "TurnStarted", None, {"data": {}}, NOW)
    malformed = TraceEvent(
        uuid4(), run_id, 1, "run_started", None,
        {"schema_version": 1, "occurred_at": "bad", "event_payload": {}}, NOW,
    )
    valid = TraceEvent(
        uuid4(), run_id, 2, "run_started", None,
        {"schema_version": 1, "occurred_at": "2026-09-16T08:00:00Z",
         "event_payload": {"run_type": "agent", "snapshot_id": str(uuid4())}}, NOW,
    )
    assert application_event_from_record(legacy) is None
    assert application_event_from_record(malformed) is None
    assert application_event_from_record(valid).sequence == 2


def test_error_category_enum_is_frozen_to_contract():
    assert {item.value for item in ApplicationEventType} == {
        "run_started", "plan_created", "context_updated", "llm_started",
        "llm_finished", "tool_started", "tool_finished", "run_completed",
        "run_failed",
    }
    assert {item.value for item in PublicErrorCategory} == {
        "configuration", "authentication", "rate_limit", "timeout",
        "invalid_response", "schema_validation", "policy_denied", "tool_error",
        "agent_error", "backend_error", "worker_lost", "unknown",
    }
