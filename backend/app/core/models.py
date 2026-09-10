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
    index_summary: IndexSummary | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class ProjectSnapshot:
    id: UUID
    project_id: UUID
    commit_sha: str
    index_artifact_path: str | None
    graph_artifact_path: str | None
    file_count: int
    node_count: int
    edge_count: int
    status: str
    created_at: datetime
    updated_at: datetime


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
