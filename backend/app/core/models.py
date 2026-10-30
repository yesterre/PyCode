from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
import re
from typing import Any, Literal
from uuid import UUID

from backend.app.core.errors import BackendError


class UserStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


def normalize_account_name(value: str) -> str:
    if not isinstance(value, str) or not value.isascii():
        raise BackendError("invalid_user", "Account name is invalid.")
    normalized = value.strip().lower()
    if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,63}", normalized) is None:
        raise BackendError("invalid_user", "Account name is invalid.")
    return normalized


def validate_user_fields(password_hash: str, status: UserStatus) -> UserStatus:
    # Persistence accepts an encoded hash, never computes or logs credentials.
    if (not isinstance(password_hash, str)
            or re.fullmatch(r"[!-~]{1,1024}", password_hash) is None):
        raise BackendError("invalid_user", "Encoded password hash is invalid.")
    try:
        return UserStatus(status)
    except (ValueError, TypeError):
        raise BackendError("invalid_user", "User status is invalid.") from None


@dataclass(frozen=True)
class User:
    id: UUID
    account_name: str
    password_hash: str = field(repr=False)
    created_at: datetime
    status: UserStatus


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
    owner_id: UUID | None = None
    archived_at: datetime | None = None

    def ensure_not_archived(self) -> None:
        if self.archived_at is not None:
            raise BackendError("project_archived", "Restore the project before starting new work.")


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


AnalysisHistoryMode = Literal["repository", "impact", "agent"]


@dataclass(frozen=True)
class AnalysisHistoryItem:
    id: UUID
    created_at: datetime
    mode: AnalysisHistoryMode
    question: str
    answer: str


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
