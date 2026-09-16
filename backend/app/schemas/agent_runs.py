from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from backend.app.core.application_trace import ApplicationEventType
from backend.app.core.models import AgentRunStatus
from backend.app.schemas.projects import NonEmptyString, RequestModel


class CreateAgentRunRequest(RequestModel):
    project_id: UUID
    task: NonEmptyString


class AgentRunAcceptedResponse(BaseModel):
    run_id: UUID
    task_id: UUID
    status: AgentRunStatus


class AgentRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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
    created_at: datetime
    started_at: datetime | None
    core_started_at: datetime | None
    finished_at: datetime | None


class ApplicationTraceEventResponse(BaseModel):
    schema_version: Literal[1]
    sequence: int
    event_type: ApplicationEventType
    occurred_at: datetime
    tool_name: str | None
    payload: dict[str, Any]


class AgentRunTraceResponse(BaseModel):
    run_id: UUID
    events: list[ApplicationTraceEventResponse]
