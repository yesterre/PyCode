from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints

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
    index_summary: IndexSummaryResponse | None
    last_error: str | None


class AnalysisResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    project_id: UUID
    answer: str
    intent: str
    evidence: list[str]


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    detail: ErrorDetail
