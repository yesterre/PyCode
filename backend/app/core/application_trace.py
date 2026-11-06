from __future__ import annotations

import json
import re
import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


APPLICATION_TRACE_SCHEMA_VERSION = 1


def public_harness_budget(value):
    """Accept a bounded, code-free execution snapshot; never project arbitrary data."""
    if not isinstance(value, dict) or set(value) != {
        'schema_version', 'limits', 'usage', 'elapsed_ms', 'stop_reason',
        'context_output_enforcement', 'transport_attempts'}:
        return None
    limits, usage = value['limits'], value['usage']
    bounds = {'max_turns': (1,128), 'max_tool_calls': (0,256), 'max_model_calls': (0,256),
              'max_query_embeddings': (0,256), 'context_tokens': (4096,1048576),
              'tool_output_chars': (256,1048576)}
    if not isinstance(limits, dict) or set(limits) != {*bounds, 'max_runtime_seconds'}:
        return None
    if any(type(limits[k]) is not int or not lo <= limits[k] <= hi for k,(lo,hi) in bounds.items()):
        return None
    seconds = limits['max_runtime_seconds']
    if type(seconds) not in (int,float) or not math.isfinite(seconds) or not 0.01 <= seconds <= 3600:
        return None
    mapping = {'turns':'max_turns','tool_calls':'max_tool_calls',
               'model_calls':'max_model_calls','query_embeddings':'max_query_embeddings'}
    if (not isinstance(usage,dict) or set(usage) != set(mapping)
            or any(type(usage[k]) is not int or not 0 <= usage[k] <= limits[v] for k,v in mapping.items())):
        return None
    elapsed = value['elapsed_ms']
    if value['stop_reason'] is not None and not isinstance(value['stop_reason'], str):
        return None
    if (type(value['schema_version']) is not int or value['schema_version'] != 1
            or type(elapsed) not in (int,float) or not math.isfinite(elapsed) or not 0 <= elapsed <= 1e12
            or value['stop_reason'] not in {None,'max_turns','budget_turns','budget_tools','budget_models',
                                          'budget_embeddings','budget_time','execution_lost'}
            or value['context_output_enforcement'] != 'reserved_for_a05c'
            or value['transport_attempts'] != 'not_observed'):
        return None
    return json.loads(json.dumps(value))


class ApplicationEventType(StrEnum):
    RUN_STARTED = "run_started"
    PLAN_CREATED = "plan_created"
    CONTEXT_UPDATED = "context_updated"
    LLM_STARTED = "llm_started"
    LLM_FINISHED = "llm_finished"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"


class LLMPurpose(StrEnum):
    ANSWER = 'answer'
    PLAN = "plan"
    NEXT_ACTION = "next_action"
    SUMMARY = "summary"
    MEMORY_SELECTION = "memory_selection"
    MEMORY_EXTRACTION = "memory_extraction"


class PublicErrorCategory(StrEnum):
    CONFIGURATION = "configuration"
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    INVALID_RESPONSE = "invalid_response"
    SCHEMA_VALIDATION = "schema_validation"
    POLICY_DENIED = "policy_denied"
    TOOL_ERROR = "tool_error"
    AGENT_ERROR = "agent_error"
    BACKEND_ERROR = "backend_error"
    WORKER_LOST = "worker_lost"
    UNKNOWN = "unknown"


class FailureStage(StrEnum):
    PLANNING = "planning"
    CONTEXT = "context"
    LLM = "llm"
    TOOL = "tool"
    SUMMARY = "summary"
    AGENT_CORE = "agent_core"
    BACKEND = "backend"


@dataclass(frozen=True)
class ApplicationError:
    category: PublicErrorCategory
    code: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"category": self.category.value}
        if self.code:
            value["code"] = sanitize_text(self.code, max_chars=128)
        if self.message:
            value["message"] = sanitize_text(self.message, max_chars=500)
        return value


@dataclass(frozen=True)
class ApplicationTraceEvent:
    sequence: int
    event_type: ApplicationEventType
    occurred_at: datetime
    tool_name: str | None
    payload: dict[str, Any]
    schema_version: int = APPLICATION_TRACE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != APPLICATION_TRACE_SCHEMA_VERSION:
            raise ValueError("Unsupported Application Trace schema version.")
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("Application Trace sequence must be non-negative.")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("Application Trace occurred_at must be timezone-aware.")
        tool_event = self.event_type in {
            ApplicationEventType.TOOL_STARTED,
            ApplicationEventType.TOOL_FINISHED,
        }
        if tool_event and (not isinstance(self.tool_name, str) or not self.tool_name.strip()):
            raise ValueError("Tool Application Events require tool_name.")
        if not tool_event and self.tool_name is not None:
            raise ValueError("Non-tool Application Events require null tool_name.")
        if not isinstance(self.payload, dict):
            raise ValueError("Application Trace payload must be an object.")
        validate_event_payload(self.event_type, self.payload)

    def database_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "occurred_at": format_utc(self.occurred_at),
            "event_payload": self.payload,
        }

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "event_type": self.event_type.value,
            "occurred_at": self.occurred_at.astimezone(timezone.utc),
            "tool_name": self.tool_name,
            "payload": self.payload,
        }


def application_event_from_record(record: Any) -> ApplicationTraceEvent | None:
    """Decode only valid v1 rows; legacy and malformed rows stay private."""
    try:
        stored = getattr(record, "payload")
        if not isinstance(stored, dict) or stored.get("schema_version") != 1:
            return None
        occurred_at = datetime.fromisoformat(
            str(stored["occurred_at"]).replace("Z", "+00:00")
        )
        event_payload = stored["event_payload"]
        return ApplicationTraceEvent(
            schema_version=1,
            sequence=int(getattr(record, "sequence")),
            event_type=ApplicationEventType(str(getattr(record, "event_type"))),
            occurred_at=occurred_at,
            tool_name=getattr(record, "tool_name"),
            payload=event_payload,
        )
    except (KeyError, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ApplicationTraceFailure:
    stage: FailureStage
    error: ApplicationError


@dataclass(frozen=True)
class ApplicationTraceNormalization:
    events: list[ApplicationTraceEvent]
    trace_complete: bool


_REQUIRED_PAYLOAD_FIELDS: dict[ApplicationEventType, set[str]] = {
    ApplicationEventType.RUN_STARTED: {"run_type", "snapshot_id"},
    ApplicationEventType.PLAN_CREATED: {"source", "fallback_used", "steps"},
    ApplicationEventType.CONTEXT_UPDATED: {"included_sections"},
    ApplicationEventType.LLM_STARTED: {"purpose"},
    ApplicationEventType.LLM_FINISHED: {"purpose", "success", "fallback_used"},
    ApplicationEventType.TOOL_STARTED: set(),
    ApplicationEventType.TOOL_FINISHED: {"outcome"},
    ApplicationEventType.RUN_COMPLETED: {"stop_reason", "has_result", "trace_complete"},
    ApplicationEventType.RUN_FAILED: {"stage", "error", "trace_complete"},
}


def validate_event_payload(event_type: ApplicationEventType, payload: dict[str, Any]) -> None:
    if 'harness_budget' in payload and (not isinstance(payload['harness_budget'], dict)
            or public_harness_budget(payload['harness_budget']) != payload['harness_budget']):
        raise ValueError('Invalid public execution budget')
    missing = _REQUIRED_PAYLOAD_FIELDS[event_type] - payload.keys()
    if missing:
        raise ValueError(f"Missing {event_type.value} payload fields: {sorted(missing)}")
    allowed = {
        ApplicationEventType.RUN_STARTED: {"run_type", "snapshot_id"},
        ApplicationEventType.PLAN_CREATED: {"source", "fallback_used", "steps"},
        ApplicationEventType.CONTEXT_UPDATED: {
            "included_sections", "turn_index", "skipped_sections", "warnings", "retrieval", "question_preflight",
            "harness_budget",
        },
        ApplicationEventType.LLM_STARTED: {"purpose", "turn_index", "model"},
        ApplicationEventType.LLM_FINISHED: {
            "purpose", "success", "fallback_used", "turn_index", "duration_ms",
            "model", "usage", "error",
        },
        ApplicationEventType.TOOL_STARTED: {"turn_index", "arguments", "reason"},
        ApplicationEventType.TOOL_FINISHED: {
            "outcome", "duration_ms", "summary", "error", "denied_by", "result_summary",
            "retrieval", "graph",
            "retrieval_origin", "initial_pack_digest",
        },
        ApplicationEventType.RUN_COMPLETED: {
            "stop_reason", "has_result", "trace_complete", "duration_ms",
            "harness_budget",
        },
        ApplicationEventType.RUN_FAILED: {
            "stage", "error", "trace_complete", "duration_ms",
            "harness_budget",
        },
    }[event_type]
    unexpected = payload.keys() - allowed
    if unexpected:
        raise ValueError(f"Unexpected {event_type.value} payload fields: {sorted(unexpected)}")

    if event_type == ApplicationEventType.RUN_STARTED:
        _require_nonempty_string(payload["run_type"], "run_type")
        _require_nonempty_string(payload["snapshot_id"], "snapshot_id")
    elif event_type == ApplicationEventType.PLAN_CREATED:
        if payload["source"] not in {"llm", "rule", "fallback"}:
            raise ValueError("Invalid plan source.")
        _require_bool(payload["fallback_used"], "fallback_used")
        if not isinstance(payload["steps"], list):
            raise ValueError("Plan steps must be a list.")
        for index, step in enumerate(payload["steps"]):
            if not isinstance(step, dict) or set(step) != {
                "step_index", "tool", "reason", "required",
            }:
                raise ValueError("Invalid plan step.")
            if type(step["step_index"]) is not int or step["step_index"] != index:
                raise ValueError("Invalid plan step_index.")
            if step["tool"] is not None and not isinstance(step["tool"], str):
                raise ValueError("Invalid plan tool.")
            if not isinstance(step["reason"], str):
                raise ValueError("Invalid plan reason.")
            _require_bool(step["required"], "required")
    elif event_type == ApplicationEventType.CONTEXT_UPDATED:
        if 'harness_budget' in payload and public_harness_budget(payload['harness_budget']) != payload['harness_budget']:
            raise ValueError('Invalid public execution budget')
        if 'question_preflight' in payload:
            from backend.app.core.run_metadata import PreflightMetadata
            if PreflightMetadata.model_validate(payload['question_preflight']).model_dump(mode='json') != payload['question_preflight']:
                raise ValueError('Invalid question preflight')
        if 'retrieval' in payload and (not isinstance(payload['retrieval'],dict)
                or public_retrieval_summary(payload['retrieval']) != payload['retrieval']):
            raise ValueError('Invalid public retrieval summary')
        _validate_context_identifiers(payload["included_sections"])
        if "skipped_sections" in payload:
            _validate_context_identifiers(payload["skipped_sections"])
        if "warnings" in payload and (
            not isinstance(payload["warnings"], list)
            or not all(isinstance(item, str) for item in payload["warnings"])
        ):
            raise ValueError("Invalid context warnings.")
        _validate_optional_turn_index(payload)
    elif event_type in {ApplicationEventType.LLM_STARTED, ApplicationEventType.LLM_FINISHED}:
        if payload["purpose"] not in {item.value for item in LLMPurpose}:
            raise ValueError("Invalid LLM purpose.")
        _validate_optional_turn_index(payload)
        if "model" in payload and not isinstance(payload["model"], str):
            raise ValueError("Invalid LLM model.")
        if event_type == ApplicationEventType.LLM_FINISHED:
            success = payload["success"]
            _require_bool(success, "success")
            _require_bool(payload["fallback_used"], "fallback_used")
            _validate_optional_duration(payload)
            if "usage" in payload and not isinstance(payload["usage"], dict):
                raise ValueError("Invalid LLM usage.")
            if success and "error" in payload:
                raise ValueError("Successful LLM event cannot contain an error.")
            if not success and "error" not in payload:
                raise ValueError("Failed LLM event requires an error.")
    elif event_type == ApplicationEventType.TOOL_STARTED:
        _validate_optional_turn_index(payload)
        if "arguments" in payload and not isinstance(payload["arguments"], dict):
            raise ValueError("Invalid tool arguments.")
        if "reason" in payload and not isinstance(payload["reason"], str):
            raise ValueError("Invalid tool reason.")
    elif event_type == ApplicationEventType.TOOL_FINISHED:
        if 'retrieval_origin' in payload or 'initial_pack_digest' in payload:
            if (payload.get('retrieval_origin') != 'question_preflight' or 'retrieval' not in payload
                or not isinstance(payload.get('initial_pack_digest'),str)
                or not re.fullmatch(r'[0-9a-f]{64}',payload['initial_pack_digest'])):
                raise ValueError('Invalid initial evidence origin')
        if payload["outcome"] not in {"success", "failed", "denied"}:
            raise ValueError("Invalid tool outcome.")
        if payload.get("denied_by") not in {None, "policy", "hook", "other"}:
            raise ValueError("Invalid tool denial source.")
        _validate_optional_duration(payload)
        for field in ("summary", "result_summary"):
            if field in payload and not isinstance(payload[field], str):
                raise ValueError(f"Invalid tool {field}.")
        if "retrieval" in payload and (not isinstance(payload["retrieval"], dict)
                                      or public_retrieval_summary(payload["retrieval"]) != payload["retrieval"]):
            raise ValueError("Invalid public retrieval summary.")
        if 'graph' in payload and (not isinstance(payload['graph'],dict) or public_graph_summary(payload['graph']) != payload['graph']):
            raise ValueError('Invalid public Graph summary.')
    elif event_type == ApplicationEventType.RUN_COMPLETED:
        _require_nonempty_string(payload["stop_reason"], "stop_reason")
        _require_bool(payload["has_result"], "has_result")
        _require_bool(payload["trace_complete"], "trace_complete")
        _validate_optional_duration(payload)
    elif event_type == ApplicationEventType.RUN_FAILED:
        if payload["stage"] not in {item.value for item in FailureStage}:
            raise ValueError("Invalid run failure stage.")
        _require_bool(payload["trace_complete"], "trace_complete")
        _validate_optional_duration(payload)
        _validate_error(payload["error"])
    if "error" in payload and event_type != ApplicationEventType.RUN_FAILED:
        _validate_error(payload["error"])
    try:
        encoded = json.dumps(
            payload, ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Application Trace payload is not JSON serializable.") from exc
    if len(encoded) > MAX_PUBLIC_PAYLOAD_BYTES:
        raise ValueError("Application Trace payload exceeds the public size limit.")


def public_graph_summary(value: Any) -> dict | None:
    """Code-free Graph Tool metadata, shared by live/final/replayed events."""
    from uuid import UUID
    if not isinstance(value,dict) or type(value.get('graph_schema_version')) is not int or value['graph_schema_version'] not in {1,2,3,4}:
        return None
    capabilities=value.get('graph_capabilities')
    allowed={'legacy_structure','lexical_scopes','call_sites','import_declarations','same_module_direct_calls','import_bindings','cross_module_calls','symbol_references','local_receiver_bindings'}
    if not isinstance(capabilities,list) or len(capabilities)>8 or any(not isinstance(c,str) or c not in allowed for c in capabilities):
        return None
    binding=value.get('binding')
    if binding is not None:
        try:
            commit=binding['commit_sha']
            if not isinstance(commit,str) or re.fullmatch('[0-9a-f]{40,64}',commit) is None:
                return None
            binding={k:str(UUID(binding[k])) for k in ('project_id','snapshot_id')} | {'commit_sha':commit}
        except (KeyError,TypeError,ValueError,AttributeError):
            return None
    result={'graph_schema_version':value['graph_schema_version'],'graph_capabilities':capabilities,'binding':binding}
    if value['graph_schema_version'] in {2,3,4}:
        counts={k:value.get(k) for k in ('call_sites_count','unknown_count','ambiguous_count')}
        if any(type(v) is not int or v<0 for v in counts.values()) or counts['unknown_count']+counts['ambiguous_count']>counts['call_sites_count']:
            return None
        result.update(counts)
    return result


def public_retrieval_summary(value: Any) -> dict | None:
    """Bounded, code-free allowlist shared by live and persisted Tool events."""
    from math import isfinite
    from uuid import UUID

    if not isinstance(value, dict):
        return None
    strategy = value.get("strategy")
    status = value.get("status")
    if strategy not in {"general", "explain", "impact", "entry", "onboard", "dependency"} or status not in {"ok", "degraded", "empty", "unavailable"}:
        return None
    binding = value.get("binding")
    if binding is not None:
        try:
            binding = {name: str(UUID(binding[name])) for name in ("project_id", "snapshot_id")}
            commit = value["binding"]["commit_sha"]
            if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40,64}", commit):
                return None
            binding["commit_sha"] = commit
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
    duration, count = value.get("duration_ms"), value.get("evidence_count")
    if type(duration) not in {int, float} or not isfinite(duration) or duration < 0 or type(count) is not int or count < 0:
        return None
    refs = value.get("references")
    if not isinstance(refs, list):
        return None
    references = []
    for reference in refs[:6]:
        if not isinstance(reference, dict):
            return None
        path = reference.get("path")
        if (not isinstance(path, str) or len(path) > 500 or "\\" in path or ":" in path
                or any(part in {"", ".", ".."} for part in path.split("/"))):
            return None
        start, end = reference.get("start_line"), reference.get("end_line")
        if (start is not None or end is not None) and (type(start) is not int or type(end) is not int or not 1 <= start <= end):
            return None
        ids = reference.get("chunk_ids", [])
        if not isinstance(ids, list) or any(not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key) for key in ids):
            return None
        source, coverage = reference.get("source"), reference.get("coverage")
        if source not in {"exact_path", "exact_symbol", "keyword", "graph", "entry", "vector", "hybrid"} or coverage not in {"complete_symbol", "complete_file", "unavailable"}:
            return None
        symbol = reference.get("symbol")
        if symbol is not None and not isinstance(symbol, str):
            return None
        omitted = reference.get("chunk_ids_omitted", 0)
        if type(omitted) is not int or omitted < 0:
            return None
        references.append({"path": sanitize_text(path), "symbol": sanitize_text(symbol, max_chars=200) if symbol else None,
                           "start_line": start, "end_line": end, "chunk_ids": ids[:4],
                           "chunk_ids_omitted": omitted + max(0, len(ids) - 4), "source": source, "coverage": coverage})
        scores = reference.get('source_scores', [])
        if not isinstance(scores, list) or len(scores) > 3:
            return None
        clean_scores = []
        for entry in scores:
            if not isinstance(entry, dict) or entry.get('source') not in {'keyword','vector','graph'}:
                return None
            score, rank, contribution = (entry.get(k) for k in ('score','rank','contribution'))
            if (type(score) not in {int,float} or not isfinite(score) or type(rank) is not int or not 1 <= rank <= 20
                    or type(contribution) not in {int,float} or not isfinite(contribution) or not 0 <= contribution <= 1):
                return None
            clean_scores.append({'source':entry['source'], 'score':score, 'rank':rank, 'contribution':contribution})
        if clean_scores:
            references[-1]['source_scores'] = clean_scores
        distance = len(reference['graph_path']) if isinstance(reference.get('graph_path'), list) else reference.get('graph_distance', 0)
        if type(distance) is not int or not 0 <= distance <= 4:
            return None
        if distance:
            references[-1].update(graph_distance=distance, relation_confidence='static_unverified')
            if value.get('graph_schema_version') in {2,3,4} or 'relation_resolution' in reference:
                steps=reference.get('graph_path')
                resolution=reference.get('relation_resolution')
                if isinstance(steps,list):
                    if any(not isinstance(s,dict) or s.get('resolution','legacy') not in ('legacy','static','declaration') for s in steps):
                        return None
                    states={s.get('resolution','legacy') for s in steps}
                    if not states.issubset({'legacy','static','declaration'}):
                        return None
                    resolution='legacy' if 'legacy' in states else 'declaration' if 'declaration' in states else 'static'
                if resolution not in {'legacy','static','declaration'}:
                    return None
                references[-1]['relation_resolution']=resolution
    omitted = value.get("references_omitted", 0)
    if type(omitted) is not int or omitted < 0:
        return None
    diagnostics = value.get("diagnostics", [])
    if not isinstance(diagnostics, list) or any(not isinstance(code, str) or not re.fullmatch(r"[a-z_]+", code) for code in diagnostics):
        return None
    result = {"strategy": strategy, "status": status, "binding": binding, "duration_ms": duration,
            "evidence_count": count, "diagnostics": diagnostics[:20], "references": references,
            "references_omitted": omitted + max(0, len(refs) - 6)}
    if value.get('graph_schema_version') is not None:
        if type(value['graph_schema_version']) is not int or value['graph_schema_version'] not in {1,2,3,4}:
            return None
        result['graph_schema_version']=value['graph_schema_version']
    if 'mode' in value:
        if value['mode'] not in {'keyword','vector','hybrid'}:
            return None
        result['mode'] = value['mode']
    pack = value.get('pack')
    if pack is not None:
        if not isinstance(pack, dict) or pack.get('estimator') != 'utf8_bytes_upper_bound_v1':
            return None
        budget, used, selected, discarded = (pack.get(k) for k in ('budget','estimated_tokens','selected_count','discarded_count'))
        if (any(type(n) is not int for n in (budget,used,selected,discarded)) or not 4096 <= budget <= 1048576
                or not 0 <= used <= budget or selected != count or discarded < 0):
            return None
        reasons = pack.get('reason_counts')
        if (not isinstance(reasons, dict) or set(reasons) - {'token_budget','item_limit','overlap','file_diversity'}
                or any(type(n) is not int or n < 0 for n in reasons.values()) or sum(reasons.values()) != discarded):
            return None
        result['pack'] = {'budget':budget, 'estimated_tokens':used, 'estimator':pack['estimator'],
                          'selected_count':selected, 'discarded_count':discarded, 'reason_counts':reasons}
    stats = value.get('graph_stats')
    if stats is not None:
        fields = {'visited_nodes','edge_checks','depth_reached','unknown_relations','node_limit','edge_limit','depth_limit','indexed_edges'}
        if (not isinstance(stats, dict) or any(type(stats.get(k)) is not int or stats[k] < 0 for k in fields)
                or type(stats.get('limited')) is not bool or stats['visited_nodes'] > stats['node_limit']
                or stats['edge_checks'] > stats['edge_limit'] or stats['depth_reached'] > stats['depth_limit']):
            return None
        result['graph_stats'] = {k:stats[k] for k in sorted(fields | {'limited'})}
    return result


def _validate_error(value: Any) -> None:
    if not isinstance(value, dict) or value.get("category") not in {
        item.value for item in PublicErrorCategory
    }:
        raise ValueError("Invalid public error.")
    if set(value) - {"category", "code", "message"}:
        raise ValueError("Unexpected public error field.")
    for field in ("code", "message"):
        if field in value and not isinstance(value[field], str):
            raise ValueError(f"Invalid public error {field}.")


def _require_bool(value: Any, field: str) -> None:
    if type(value) is not bool:
        raise ValueError(f"Invalid {field}.")


def _require_nonempty_string(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Invalid {field}.")


def _validate_optional_turn_index(payload: dict[str, Any]) -> None:
    if "turn_index" in payload and (
        type(payload["turn_index"]) is not int or payload["turn_index"] < 0
    ):
        raise ValueError("Invalid turn_index.")


def _validate_optional_duration(payload: dict[str, Any]) -> None:
    if "duration_ms" in payload and (
        type(payload["duration_ms"]) not in {int, float}
        or payload["duration_ms"] < 0
    ):
        raise ValueError("Invalid duration_ms.")


def _validate_context_identifiers(value: Any) -> None:
    if (
        not isinstance(value, list)
        or len(value) != len(set(value))
        or not all(item in PUBLIC_CONTEXT_ORDER for item in value)
    ):
        raise ValueError("Invalid public context identifiers.")


SENSITIVE_KEYS = {
    "token", "secret", "password", "authorization", "api_key", "apikey", "access_key",
}
MAX_PUBLIC_STRING_CHARS = 500
MAX_PUBLIC_LIST_ITEMS = 20
MAX_PUBLIC_OBJECT_ITEMS = 30
MAX_PUBLIC_DEPTH = 5
MAX_PUBLIC_PAYLOAD_BYTES = 16_384
_SECRET_PATTERN = re.compile(
    r"(?i)(bearer\s+)[^\s,;]+|((?:api[_-]?key|token|password|secret)\s*[:=]\s*)[^\s,;]+"
)


def sanitize_text(value: Any, *, max_chars: int = MAX_PUBLIC_STRING_CHARS) -> str:
    text = str(value)
    text = _SECRET_PATTERN.sub(lambda match: (match.group(1) or match.group(2)) + "[REDACTED]", text)
    if len(text) > max_chars:
        return text[:max_chars] + "...[truncated]"
    return text


def sanitize_public_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= MAX_PUBLIC_DEPTH:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else str(value)
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_PUBLIC_OBJECT_ITEMS:
                break
            text_key = sanitize_text(key, max_chars=128)
            normalized = text_key.lower().replace("-", "_")
            result[text_key] = (
                "[REDACTED]" if normalized in SENSITIVE_KEYS
                else sanitize_public_value(item, depth=depth + 1)
            )
        return result
    if isinstance(value, (list, tuple)):
        return [
            sanitize_public_value(item, depth=depth + 1)
            for item in value[:MAX_PUBLIC_LIST_ITEMS]
        ]
    return sanitize_text(value)


def sanitize_arguments(value: Any) -> dict[str, Any] | None:
    try:
        sanitized = sanitize_public_value(value)
        if not isinstance(sanitized, dict):
            return None
        encoded = json.dumps(sanitized, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return sanitized if len(encoded) <= MAX_PUBLIC_PAYLOAD_BYTES else None
    except Exception:
        return None


_ERROR_CATEGORY_MAP = {
    "execution_stopped": PublicErrorCategory.AGENT_ERROR,
    "missing_config": PublicErrorCategory.CONFIGURATION,
    "unsupported_api_type": PublicErrorCategory.CONFIGURATION,
    "dependency_missing": PublicErrorCategory.CONFIGURATION,
    "auth_failed": PublicErrorCategory.AUTHENTICATION,
    "rate_limited": PublicErrorCategory.RATE_LIMIT,
    "timeout": PublicErrorCategory.TIMEOUT,
    "invalid_response": PublicErrorCategory.INVALID_RESPONSE,
    "schema_parse_failed": PublicErrorCategory.SCHEMA_VALIDATION,
    "schema_validation": PublicErrorCategory.SCHEMA_VALIDATION,
    "policy_denied": PublicErrorCategory.POLICY_DENIED,
    "tool_error": PublicErrorCategory.TOOL_ERROR,
    "agent_run_failed": PublicErrorCategory.AGENT_ERROR,
    "agent_error": PublicErrorCategory.AGENT_ERROR,
    "worker_lost_after_agent_core": PublicErrorCategory.WORKER_LOST,
    "worker_execution_lost": PublicErrorCategory.WORKER_LOST,
}
_SAFE_ERROR_MESSAGES = {
    "worker_lost_after_agent_core": "Worker execution was lost after Agent Core started.",
    "worker_execution_lost": "Worker execution ownership was lost before completion.",
    "agent_run_failed": "Agent execution did not complete successfully.",
}


def normalize_error(
    category: Any = None, *, code: Any = None, safe_message: str | None = None,
) -> ApplicationError:
    raw_category = str(category or "unknown")
    public_category = _ERROR_CATEGORY_MAP.get(raw_category)
    if public_category is None:
        try:
            public_category = PublicErrorCategory(raw_category)
        except ValueError:
            public_category = PublicErrorCategory.UNKNOWN
    safe_code = sanitize_text(code, max_chars=128) if code else None
    message = safe_message or _SAFE_ERROR_MESSAGES.get(safe_code or "")
    return ApplicationError(public_category, safe_code, message)


PUBLIC_CONTEXT_ORDER = (
    "system", "project", "plan", "tool_results", "evidence", "task_state",
    "memory", "execution_history",
)
_CONTEXT_MAP = {
    "identity": "system", "tools": "system", "policy": "system",
    "response_contract": "system", "project": "project", "plan": "plan",
    "tool_results": "tool_results", "evidence": "evidence", "tasks": "task_state",
    "todo": "task_state", "memory": "memory", "memory_index": "memory",
    "trace": "execution_history", 'initial_evidence': 'evidence',
}


class ApplicationTraceNormalizer:
    """Project internal Agent observations into the stable v1 public contract."""

    def normalize(
        self,
        run: Any,
        *,
        result: Any | None = None,
        failure: ApplicationTraceFailure | None = None,
    ) -> ApplicationTraceNormalization:
        drafts: list[tuple[ApplicationEventType, datetime, str | None, dict[str, Any]]] = []
        complete = result is not None and getattr(result, "trace", None) is not None

        started_at = getattr(run, "started_at", None)
        snapshot_id = getattr(run, "snapshot_id", None)
        if _aware(started_at) and snapshot_id is not None:
            drafts.append((
                ApplicationEventType.RUN_STARTED, started_at, None,
                {"run_type": str(getattr(run, "run_type", "agent")), "snapshot_id": str(snapshot_id)},
            ))
        else:
            complete = False

        trace = getattr(result, "trace", None) if result is not None else None
        internal_events, events_available = _safe_collection(trace, "events")
        tools, tools_available = _safe_collection(trace, "tools")
        if trace is not None and (not events_available or not tools_available):
            complete = False
        tool_traces = _tool_trace_index(tools)
        reasons = _tool_reasons(internal_events)
        plan_emitted = False
        open_llm_calls: dict[tuple[str, Any], int] = {}

        for internal in internal_events:
            try:
                kind = str(getattr(internal, "event_type", ""))
                occurred = getattr(internal, "timestamp", None)
                if not _aware(occurred):
                    if kind in _KEY_INTERNAL_EVENTS:
                        complete = False
                    continue
                data = getattr(internal, "data", {})
                if not isinstance(data, dict):
                    data = {}

                if kind in {"LLMPlanGenerated", "LLMPlanFallback", "PlannerSelected"} and not plan_emitted:
                    plan_payload = self._plan_payload(kind, result)
                    drafts.append((ApplicationEventType.PLAN_CREATED, occurred, None, plan_payload))
                    plan_emitted = True
                elif kind == "ExecutionBudget":
                    continue  # Snapshot is attached to the existing terminal event below.
                elif kind == "ContextAssembled":
                    if not isinstance(data.get("included_section_names"), list) or not isinstance(
                        data.get("skipped_section_names"), list
                    ):
                        complete = False
                        continue
                    drafts.append((
                        ApplicationEventType.CONTEXT_UPDATED, occurred, None,
                        self._context_payload(data),
                    ))
                elif kind == "LLMCallStarted":
                    purpose = str(data.get("purpose", ""))
                    payload: dict[str, Any] = {"purpose": purpose}
                    if isinstance(data.get("turn_index"), int):
                        payload["turn_index"] = data["turn_index"]
                    drafts.append((ApplicationEventType.LLM_STARTED, occurred, None, payload))
                    open_llm_calls[(purpose, data.get("turn_index"))] = (
                        open_llm_calls.get((purpose, data.get("turn_index")), 0) + 1
                    )
                elif kind == "LLMCallFinished":
                    purpose = str(data.get("purpose", ""))
                    success = bool(data.get("success"))
                    payload = {
                        "purpose": purpose,
                        "success": success,
                        "fallback_used": bool(data.get("fallback_used", False)),
                    }
                    if isinstance(data.get("turn_index"), int):
                        payload["turn_index"] = data["turn_index"]
                    if not success:
                        payload["error"] = normalize_error(
                            data.get("error_category"), code=data.get("error_code"),
                        ).to_dict()
                    drafts.append((ApplicationEventType.LLM_FINISHED, occurred, None, payload))
                    key = (purpose, data.get("turn_index"))
                    if open_llm_calls.get(key, 0) > 0:
                        open_llm_calls[key] -= 1
                    else:
                        complete = False
                elif kind in {"PreToolUse", "PostToolUse"}:
                    key = (data.get("turn_index"), str(data.get("tool", "")))
                    tool = tool_traces.get(key)
                    if tool is None:
                        complete = False
                        continue
                    if kind == "PreToolUse":
                        drafts.append(self._tool_started(tool, reasons.get(key)))
                    else:
                        drafts.append(self._tool_finished(tool))
            except Exception:
                complete = False

        if any(count for count in open_llm_calls.values()):
            complete = False
        observed_starts, observed_finishes, expected_llm, counts_valid = (
            _llm_observation_counts(internal_events)
        )
        if not counts_valid:
            complete = False
        if any(
            observed_starts[purpose] < count or observed_finishes[purpose] < count
            for purpose, count in expected_llm.items()
        ):
            complete = False

        terminal_at = getattr(run, "finished_at", None)
        status = str(getattr(run, "status", ""))
        if not _aware(terminal_at):
            complete = False
        elif status == "completed":
            payload = {
                "stop_reason": str(
                    getattr(result, "stop_reason", None)
                    or getattr(trace, "stop_reason", None)
                    or "final"
                ),
                "has_result": getattr(run, "result", None) is not None,
                "trace_complete": complete,
            }
            duration = _duration_ms(started_at, terminal_at)
            if duration is not None:
                payload["duration_ms"] = duration
            drafts.append((ApplicationEventType.RUN_COMPLETED, terminal_at, None, payload))
        elif status == "failed":
            complete = False if result is None else complete
            resolved = failure or ApplicationTraceFailure(
                FailureStage.AGENT_CORE,
                normalize_error("agent_error", code="agent_run_failed"),
            )
            payload = {
                "stage": resolved.stage.value,
                "error": resolved.error.to_dict(),
                "trace_complete": complete,
            }
            duration = _duration_ms(started_at, terminal_at)
            if duration is not None:
                payload["duration_ms"] = duration
            drafts.append((ApplicationEventType.RUN_FAILED, terminal_at, None, payload))

        budget = next((value for event in reversed(internal_events)
                       if isinstance(data := getattr(event, 'data', None), dict)
                       and (value := public_harness_budget(data.get('harness_budget'))) is not None), None)
        if drafts and budget is not None and drafts[-1][0] in {
                ApplicationEventType.RUN_COMPLETED, ApplicationEventType.RUN_FAILED}:
            drafts[-1][3]['harness_budget'] = budget
        events: list[ApplicationTraceEvent] = []
        for event_type, occurred_at, tool_name, payload in drafts:
            try:
                events.append(ApplicationTraceEvent(
                    sequence=len(events), event_type=event_type,
                    occurred_at=occurred_at, tool_name=tool_name, payload=payload,
                ))
            except Exception:
                complete = False

        # A late validation failure must be reflected by the terminal event.
        if events and events[-1].event_type in {
            ApplicationEventType.RUN_COMPLETED, ApplicationEventType.RUN_FAILED,
        } and events[-1].payload.get("trace_complete") != complete:
            terminal = events[-1]
            payload = dict(terminal.payload)
            payload["trace_complete"] = complete
            events[-1] = ApplicationTraceEvent(
                terminal.sequence, terminal.event_type, terminal.occurred_at,
                terminal.tool_name, payload,
            )
        return ApplicationTraceNormalization(events, complete)

    @staticmethod
    def _plan_payload(kind: str, result: Any) -> dict[str, Any]:
        return ApplicationTraceNormalizer.plan_payload_from_steps(kind, getattr(result, "steps", []))

    @staticmethod
    def plan_payload_from_steps(kind: str, raw_steps: Any) -> dict[str, Any]:
        source, fallback = {
            "LLMPlanGenerated": ("llm", False),
            "LLMPlanFallback": ("fallback", True),
            "PlannerSelected": ("rule", False),
        }[kind]
        steps = []
        for index, step in enumerate(list(raw_steps or [])):
            tool = getattr(step, "tool", None)
            steps.append({
                "step_index": index,
                "tool": str(tool) if tool is not None else None,
                "reason": sanitize_text(getattr(step, "reason", "")),
                "required": bool(getattr(step, "required", False)),
            })
        return {"source": source, "fallback_used": fallback, "steps": steps}

    @staticmethod
    def _context_payload(data: dict[str, Any]) -> dict[str, Any]:
        included = _public_context_sections(data.get("included_section_names", []))
        included_set = set(included)
        skipped = [
            section
            for section in _public_context_sections(
                data.get("skipped_section_names", [])
            )
            if section not in included_set
        ]
        payload: dict[str, Any] = {"included_sections": included}
        budget = public_harness_budget(data.get('harness_budget'))
        if budget is not None:
            payload['harness_budget'] = budget
        if isinstance(data.get("turn_index"), int):
            payload["turn_index"] = data["turn_index"]
        if skipped:
            payload["skipped_sections"] = skipped
        warnings = data.get("warnings")
        if isinstance(warnings, list):
            payload["warnings"] = [sanitize_text(item) for item in warnings[:MAX_PUBLIC_LIST_ITEMS]]
        retrieval = public_retrieval_summary(data.get('retrieval'))
        if retrieval is not None:
            payload['retrieval'] = retrieval
        if 'question_preflight' in data:
            from backend.app.core.run_metadata import PreflightMetadata
            payload['question_preflight'] = PreflightMetadata.model_validate(data['question_preflight']).model_dump(mode='json')
        return payload

    @staticmethod
    def _tool_started(tool: Any, reason: Any) -> tuple[ApplicationEventType, datetime, str, dict[str, Any]]:
        payload: dict[str, Any] = {}
        turn_index = getattr(tool, "turn_index", None)
        if isinstance(turn_index, int):
            payload["turn_index"] = turn_index
        arguments = sanitize_arguments(getattr(tool, "arguments", None))
        if arguments is not None:
            payload["arguments"] = arguments
        if reason:
            payload["reason"] = sanitize_text(reason)
        return (
            ApplicationEventType.TOOL_STARTED, getattr(tool, "started_at"),
            str(getattr(tool, "tool")), payload,
        )

    @staticmethod
    def _tool_finished(tool: Any) -> tuple[ApplicationEventType, datetime, str, dict[str, Any]]:
        status = str(getattr(tool, "status", "failed"))
        outcome = {"ok": "success", "failed": "failed", "denied": "denied"}.get(status, "failed")
        payload: dict[str, Any] = {"outcome": outcome}
        duration = getattr(tool, "duration_ms", None)
        if isinstance(duration, (int, float)) and duration >= 0:
            payload["duration_ms"] = duration
        summary = getattr(tool, "summary", None)
        if summary:
            payload["summary"] = sanitize_text(summary)
        if str(getattr(tool, "tool", "")) == "retrieve_context":
            retrieval = public_retrieval_summary(getattr(tool, "retrieval", None))
            if retrieval is not None:
                payload["retrieval"] = retrieval
                digest = getattr(tool, 'initial_pack_digest', None)
                if isinstance(digest,str) and re.fullmatch(r'[0-9a-f]{64}',digest):
                    payload.update(retrieval_origin='question_preflight', initial_pack_digest=digest)
        if str(getattr(tool,'tool',''))=='query_graph':
            graph=public_graph_summary(getattr(tool,'graph',None))
            if graph is not None:
                payload['graph']=graph
        if outcome != "success":
            category = "policy_denied" if outcome == "denied" else "tool_error"
            payload["error"] = normalize_error(category).to_dict()
        if outcome == "denied":
            denied_by = str(getattr(tool, "denied_by", "") or "")
            payload["denied_by"] = (
                "policy" if denied_by == "policy" else "hook" if "hook" in denied_by else "other"
            )
        return (
            ApplicationEventType.TOOL_FINISHED, getattr(tool, "ended_at"),
            str(getattr(tool, "tool")), payload,
        )


_KEY_INTERNAL_EVENTS = {
    "LLMPlanGenerated", "LLMPlanFallback", "PlannerSelected", "ContextAssembled", "ExecutionBudget",
    "LLMCallStarted", "LLMCallFinished", "PreToolUse", "PostToolUse",
}


def _safe_collection(owner: Any, name: str) -> tuple[list[Any], bool]:
    if owner is None:
        return [], False
    try:
        return list(getattr(owner, name)), True
    except Exception:
        return [], False


def _aware(value: Any) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _duration_ms(started: Any, finished: Any) -> float | None:
    if not _aware(started) or not _aware(finished):
        return None
    return round(max(0.0, (finished - started).total_seconds() * 1000), 3)


def format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _public_context_sections(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    selected = {_CONTEXT_MAP[value] for value in values if value in _CONTEXT_MAP}
    return [name for name in PUBLIC_CONTEXT_ORDER if name in selected]


def _tool_trace_index(tools: list[Any]) -> dict[tuple[Any, str], Any]:
    result: dict[tuple[Any, str], Any] = {}
    for tool in tools:
        try:
            key = (getattr(tool, "turn_index", None), str(getattr(tool, "tool", "")))
            if key in result:
                # Ambiguous traces are deliberately left unmatched.
                result[key] = None
            else:
                result[key] = tool
        except Exception:
            continue
    return result


def _tool_reasons(events: list[Any]) -> dict[tuple[Any, str], str]:
    reasons: dict[tuple[Any, str], str] = {}
    for event in events:
        try:
            if str(getattr(event, "event_type", "")) != "NextActionDecided":
                continue
            data = getattr(event, "data", {})
            if isinstance(data, dict) and data.get("tool"):
                reasons[(data.get("turn_index"), str(data["tool"]))] = str(data.get("reason") or "")
        except Exception:
            continue
    return reasons


def _llm_observation_counts(
    events: list[Any],
) -> tuple[Counter, Counter, Counter, bool]:
    starts: Counter = Counter()
    finishes: Counter = Counter()
    expected: Counter = Counter()
    valid = True
    for event in events:
        try:
            kind = str(getattr(event, "event_type", ""))
            data = getattr(event, "data", {})
            if kind in {"LLMCallStarted", "LLMCallFinished"}:
                if not isinstance(data, dict):
                    valid = False
                    continue
                target = starts if kind == "LLMCallStarted" else finishes
                target[str(data.get("purpose", ""))] += 1
            if kind in {"LLMPlanGenerated", "LLMPlanFallback"}:
                expected[LLMPurpose.PLAN.value] += 1
            elif kind == "LLMNextActionStarted":
                expected[LLMPurpose.NEXT_ACTION.value] += 1
            elif kind == "LLMSummaryFailed":
                expected[LLMPurpose.SUMMARY.value] += 1
            elif kind == "MemoryRelevantLoaded":
                expected[LLMPurpose.MEMORY_SELECTION.value] += 1
            elif kind == "MemoryExtracted":
                expected[LLMPurpose.MEMORY_EXTRACTION.value] += 1
        except Exception:
            valid = False
    return starts, finishes, expected, valid
