from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from backend.app.core.models import BackgroundTaskStatus


class BackgroundTaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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


class RepositoryIndexTaskResponse(BaseModel):
    task_id: UUID
    project_id: UUID
    status: BackgroundTaskStatus
