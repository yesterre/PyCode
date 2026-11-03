from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints, Field

from backend.app.core.models import ProjectStatus


NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateProjectRequest(RequestModel):
    name: NonEmptyString
    repo_url: NonEmptyString
    branch: NonEmptyString | None = None


class AskRequest(RequestModel):
    question: NonEmptyString
    model: NonEmptyString | None = None


class ImpactRequest(RequestModel):
    file_path: NonEmptyString
    model: NonEmptyString | None = None


class IndexSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    file_count: int
    node_count: int
    edge_count: int


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    repo_url: str
    branch: str | None
    status: ProjectStatus
    created_at: datetime
    updated_at: datetime
    current_snapshot_id: UUID | None
    index_summary: IndexSummaryResponse | None
    last_error: str | None
    archived_at: datetime | None


class ProjectListResponse(BaseModel):
    projects: list[ProjectResponse]
    has_more: bool


class SourceScoreResponse(BaseModel):
    source: Literal['keyword','vector','graph']
    score: float
    rank: int
    contribution: float


class GraphStepResponse(BaseModel):
    source: str
    target: str
    relation: str
    direction: Literal['incoming','outgoing']


class EvidencePackResponse(BaseModel):
    budget: int
    estimated_tokens: int
    estimator: str
    selected_count: int
    discarded_count: int
    discarded: list[dict]
    discarded_omitted: int
    reason_counts: dict[str,int]


class EvidenceReferenceResponse(BaseModel):
    path: str | None
    symbol: str | None
    start_line: int | None
    end_line: int | None
    chunk_ids: list[str]
    source: str
    score: float
    reason: str
    role: str
    coverage: str
    diagnostics: list[str]
    source_scores: list[SourceScoreResponse] = Field(default_factory=list)
    graph_path: list[GraphStepResponse] = Field(default_factory=list)


class RetrievalBindingResponse(BaseModel):
    project_id: UUID
    snapshot_id: UUID
    commit_sha: str


class RetrievalSummaryResponse(BaseModel):
    strategy: str
    binding: RetrievalBindingResponse | None
    status: Literal["ok", "degraded", "empty", "unavailable"]
    diagnostics: list[str]
    duration_ms: float
    evidence_count: int
    references: list[EvidenceReferenceResponse]
    mode: Literal['keyword','vector','hybrid'] = 'keyword'
    pack: EvidencePackResponse | None = None
    graph_stats: dict | None = None


class AnalysisResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    project_id: UUID
    answer: str
    intent: str
    evidence: list[str]
    retrieval: RetrievalSummaryResponse | None = None


class AnalysisHistoryItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    mode: Literal["repository", "impact", "agent"]
    question: str
    answer: str


class AnalysisHistoryResponse(BaseModel):
    items: list[AnalysisHistoryItem]
    has_more: bool


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    detail: ErrorDetail
