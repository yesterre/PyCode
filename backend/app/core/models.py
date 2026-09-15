from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class ProjectStatus(StrEnum):
    CREATED = "created"
    PREPARING = "preparing"
    INDEXING = "indexing"
    READY = "ready"
    FAILED = "failed"


class AgentRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentCoreStartStatus(StrEnum):
    MARKED = "marked"
    ALREADY_MARKED = "already_marked"
    INVALID_STATE = "invalid_state"
    NOT_FOUND = "not_found"


class BackgroundTaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class BackgroundTaskClaimStatus(StrEnum):
    CLAIMED = "claimed"
    ALREADY_RUNNING = "already_running"
    TERMINAL = "terminal"
    NOT_FOUND = "not_found"


class SnapshotStatus(StrEnum):
    INDEXING = "indexing"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True)
class IndexSummary:
    file_count: int
    node_count: int
    edge_count: int


@dataclass(frozen=True)
class Project:
    id: UUID
    name: str
    repo_url: str
    branch: str | None
    status: ProjectStatus
    created_at: datetime
    updated_at: datetime
    workspace_path: str | None = None
    current_snapshot_id: UUID | None = None
    index_summary: IndexSummary | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class ProjectSnapshot:
    id: UUID
    project_id: UUID
    commit_sha: str
    index_artifact_path: str | None
    graph_artifact_path: str | None
    file_count: int | None
    node_count: int | None
    edge_count: int | None
    status: str
    created_at: datetime
    updated_at: datetime
    error_message: str | None = None


@dataclass(frozen=True)
class AgentRun:
    id: UUID
    project_id: UUID
    snapshot_id: UUID | None
    task: str
    run_type: str
    status: AgentRunStatus
    model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    result: str | None
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    core_started_at: datetime | None = None


@dataclass(frozen=True)
class AgentCoreStart:
    status: AgentCoreStartStatus
    run: AgentRun | None


@dataclass(frozen=True)
class TraceEvent:
    id: UUID
    run_id: UUID
    sequence: int
    event_type: str
    tool_name: str | None
    payload: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class BackgroundTask:
    id: UUID
    task_type: str
    status: BackgroundTaskStatus
    project_id: UUID | None
    snapshot_id: UUID | None
    agent_run_id: UUID | None
    attempt_count: int
    error_code: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    idempotency_key: str | None = None
    request_fingerprint: str | None = None
    enqueue_attempt_count: int = 0
    last_enqueue_attempt_at: datetime | None = None
    enqueue_confirmed_at: datetime | None = None
    next_enqueue_attempt_at: datetime | None = None
    execution_token: UUID | None = None
    lease_expires_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    worker_id: str | None = None


@dataclass(frozen=True)
class BackgroundTaskClaim:
    status: BackgroundTaskClaimStatus
    task: BackgroundTask | None


@dataclass(frozen=True)
class BackgroundTaskCreation:
    task: BackgroundTask
    created: bool


@dataclass(frozen=True)
class Analysis:
    answer: str
    intent: str
    evidence: list[str]


@dataclass(frozen=True)
class ProjectAnalysis:
    project_id: UUID
    answer: str
    intent: str
    evidence: list[str]
