from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class ProjectStatus(StrEnum):
    CREATED = "created"
    PREPARING = "preparing"
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
    index_summary: IndexSummary | None = None
    last_error: str | None = None


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
