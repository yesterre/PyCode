from datetime import datetime
from uuid import UUID

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
