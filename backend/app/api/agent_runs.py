from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from backend.app.api.dependencies import get_agent_run_service
from backend.app.api.idempotency import IdempotencyKeyHeader
from backend.app.schemas.agent_runs import (
    AgentRunAcceptedResponse,
    AgentRunResponse,
    CreateAgentRunRequest,
)
from backend.app.schemas.projects import ErrorResponse
from backend.app.services.agent_runs import AgentRunService


Service = Annotated[AgentRunService, Depends(get_agent_run_service)]
router = APIRouter(
    prefix="/api/v1/agent-runs",
    tags=["agent-runs"],
    responses={code: {"model": ErrorResponse} for code in (404, 409, 500, 503)},
)


@router.post("", status_code=202, response_model=AgentRunAcceptedResponse)
def create_agent_run(
    body: CreateAgentRunRequest, service: Service,
    idempotency_key: IdempotencyKeyHeader = None,
):
    submission = service.submit(body.project_id, body.task, idempotency_key)
    return AgentRunAcceptedResponse(
        run_id=submission.run.id,
        task_id=submission.background_task.id,
        status=submission.run.status,
    )


@router.get("/{run_id}", response_model=AgentRunResponse)
def get_agent_run(run_id: UUID, service: Service):
    return service.get(run_id)
