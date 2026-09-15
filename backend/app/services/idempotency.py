from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def request_fingerprint(payload: Mapping[str, Any]) -> str:
    """Return a deterministic SHA-256 fingerprint for a validated request."""
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def repository_index_fingerprint(project_id: object) -> str:
    return request_fingerprint({
        "operation": "repository_index",
        "project_id": str(project_id),
    })


def agent_run_fingerprint(project_id: object, task: str) -> str:
    return request_fingerprint({
        "operation": "agent_run",
        "project_id": str(project_id),
        "task": task,
    })
