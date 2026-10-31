"""Process-local HTTP metrics and durable, delivery-independent task gauges."""

from collections import defaultdict
import hmac
import json
from threading import Lock

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from backend.app.api.dependencies import get_background_task_service
from backend.app.repositories.interfaces import METRIC_TASK_STATUSES as TASK_STATUSES, METRIC_TASK_TYPES as TASK_TYPES


BUCKETS = (0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 30.0, 120.0)


def labels(**values):
    return "{" + ",".join(f"{key}={json.dumps(str(value))}" for key, value in values.items()) + "}"


class RequestMetrics:
    def __init__(self):
        self._lock = Lock()
        self._requests = defaultdict(int)
        self._latency = {}

    def observe(self, route, method, status, duration):
        with self._lock:
            # Route comes from the router template, never the raw URL.
            self._requests[route, method, f"{status // 100}xx"] += 1
            histogram = self._latency.setdefault((route, method), [0, 0.0, [0] * len(BUCKETS)])
            histogram[0] += 1
            histogram[1] += duration
            for index, bound in enumerate(BUCKETS):
                histogram[2][index] += duration <= bound

    def render(self):
        lines = [
            "# HELP pycode_http_requests_total Requests in this API process since startup.",
            "# TYPE pycode_http_requests_total counter",
        ]
        with self._lock:
            for (route, method, status_class), count in sorted(self._requests.items()):
                lines.append(f"pycode_http_requests_total{labels(route=route, method=method, status_class=status_class)} {count}")
            lines.extend([
                "# HELP pycode_http_request_duration_seconds Full response duration in this API process, including SSE lifetime.",
                "# TYPE pycode_http_request_duration_seconds histogram",
            ])
            for (route, method), (count, total, buckets) in sorted(self._latency.items()):
                for bound, value in [*zip(BUCKETS, buckets), ("+Inf", count)]:
                    lines.append(f"pycode_http_request_duration_seconds_bucket{labels(route=route, method=method, le=bound)} {value}")
                for suffix, value in (("count", count), ("sum", total)):
                    lines.append(f"pycode_http_request_duration_seconds_{suffix}{labels(route=route, method=method)} {value}")
        return lines


def task_metrics(rows):
    lines = [
        "# HELP pycode_background_tasks Current durable task counts, including completed and failed outcomes.",
        "# TYPE pycode_background_tasks gauge",
        "# HELP pycode_task_queue_wait_seconds_sum Sum of latest claim waits (created_at to started_at), not all retry attempts.",
        "# TYPE pycode_task_queue_wait_seconds_sum gauge",
        "# HELP pycode_task_queue_wait_seconds_count Tasks with a latest claim timestamp.",
        "# TYPE pycode_task_queue_wait_seconds_count gauge",
        "# HELP pycode_task_queue_oldest_age_seconds Age of the oldest currently queued task.",
        "# TYPE pycode_task_queue_oldest_age_seconds gauge",
    ]
    indexed = {(row["task_type"], row["status"]): row for row in rows}
    for kind in TASK_TYPES:
        count, total, oldest = 0, 0.0, 0.0
        for status in TASK_STATUSES:
            row = indexed.get((kind, status), {})
            lines.append(f"pycode_background_tasks{labels(task_type=kind, status=status)} {row.get('count', 0)}")
            count += row.get("started_count", 0)
            total += row.get("wait_sum", 0.0)
            oldest = max(oldest, row.get("oldest_age", 0.0))
        for name, value in (("wait_seconds_sum", total), ("wait_seconds_count", count), ("oldest_age_seconds", oldest)):
            lines.append(f"pycode_task_queue_{name}{labels(task_type=kind)} {value}")
    return lines


def require_metrics_token(request: Request):
    secret = request.app.state.observability_settings.metrics_token
    if not secret:
        raise HTTPException(404, "Not Found")
    supplied = request.headers.get("Authorization", "")
    if len(supplied) > 1024 or not hmac.compare_digest(
        supplied.encode("utf-8"), f"Bearer {secret}".encode("utf-8"),
    ):
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})


router = APIRouter()


@router.get("/metrics", include_in_schema=False)
def metrics(request: Request, access=Depends(require_metrics_token), service=Depends(get_background_task_service)):
    with service.unit_of_work.transaction():
        rows = service.unit_of_work.background_tasks.metrics_snapshot()
    return Response("\n".join(request.app.state.request_metrics.render() + task_metrics(rows)) + "\n",
                    media_type="text/plain; version=0.0.4", headers={"Cache-Control": "no-store"})
