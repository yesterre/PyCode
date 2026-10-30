from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from backend.app.api.dependencies import get_agent_run_service, get_current_user
from backend.app.core.models import User
from backend.app.api.idempotency import IdempotencyKeyHeader
from backend.app.schemas.agent_runs import (
    AgentRunAcceptedResponse,
    AgentRunResponse,
    AgentRunTraceResponse,
    ApplicationTraceEventResponse,
    CreateAgentRunRequest,
)
from backend.app.schemas.projects import ErrorResponse
from backend.app.services.agent_runs import AgentRunService
from backend.app.api.realtime_trace import stream_agent_run_events


Service = Annotated[AgentRunService, Depends(get_agent_run_service)]
CurrentUser = Annotated[User, Depends(get_current_user)]
router = APIRouter(
    prefix="/api/v1/agent-runs",
    tags=["agent-runs"],
    responses={code: {"model": ErrorResponse} for code in (404, 409, 500, 503)},
)


@router.post("", status_code=202, response_model=AgentRunAcceptedResponse, responses={401: {"model": ErrorResponse}})
def create_agent_run(
    body: CreateAgentRunRequest, user: CurrentUser, service: Service,
    idempotency_key: IdempotencyKeyHeader = None,
):
    submission = service.submit(body.project_id, body.task, idempotency_key, owner_id=user.id)
    return AgentRunAcceptedResponse(
        run_id=submission.run.id,
        task_id=submission.background_task.id,
        status=submission.run.status,
    )


@router.get("/{run_id}", response_model=AgentRunResponse, responses={401: {"model": ErrorResponse}})
def get_agent_run(run_id: UUID, user: CurrentUser, service: Service):
    return service.get_owned(run_id, user.id)


@router.get("/{run_id}/trace", response_model=AgentRunTraceResponse, responses={401: {"model": ErrorResponse}})
def get_agent_run_trace(run_id: UUID, user: CurrentUser, service: Service):
    events = service.get_trace(run_id, owner_id=user.id)
    return AgentRunTraceResponse(
        run_id=run_id,
        events=[
            ApplicationTraceEventResponse.model_validate(event.to_api_dict())
            for event in events
        ],
    )


@router.get("/{run_id}/events", responses={401: {"model": ErrorResponse}})
async def get_agent_run_events(run_id: UUID, request: Request, user: CurrentUser):
    return await stream_agent_run_events(request, run_id, owner_id=user.id)
