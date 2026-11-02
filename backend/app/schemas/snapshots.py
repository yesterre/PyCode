from datetime import datetime
from uuid import UUID
from typing import Literal

from pydantic import BaseModel, ConfigDict


class SnapshotResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    project_id: UUID
    commit_sha: str
    status: str
    file_count: int | None
    node_count: int | None
    edge_count: int | None
    created_at: datetime
    updated_at: datetime
    error_message: str | None


class SnapshotListResponse(BaseModel):
    snapshots: list[SnapshotResponse]


class SemanticArtifactResponse(BaseModel):
    format_version: int
    dimension_source: Literal['validated_response', 'model_spec_empty']
    dtype: str
    byte_order: str
    vector_count: int
    byte_count: int
    sha256: str


class SemanticVersionResponse(BaseModel):
    provider: str
    model: str
    dimension: int
    fingerprint: str
    artifact: SemanticArtifactResponse
    built_at: datetime


class SemanticIndexResponse(BaseModel):
    status: Literal['disabled', 'not_built', 'queued', 'building', 'ready', 'failed', 'unavailable']
    provider: str | None = None
    model: str | None = None
    dimension: int | None = None
    fingerprint: str | None = None
    artifact: SemanticArtifactResponse | None = None
    built_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    versions: list[SemanticVersionResponse]


class SnapshotDetailResponse(SnapshotResponse):
    semantic_index: SemanticIndexResponse
