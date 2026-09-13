from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from backend.app.api.dependencies import get_background_task_service
from backend.app.schemas.projects import ErrorResponse
from backend.app.schemas.tasks import BackgroundTaskResponse
from backend.app.services.background_tasks import BackgroundTaskService


Service = Annotated[BackgroundTaskService, Depends(get_background_task_service)]
router = APIRouter(
    prefix="/api/v1/tasks", tags=["tasks"],
    responses={code: {"model": ErrorResponse} for code in (404, 409, 500, 503)},
)


@router.post("/health", status_code=202, response_model=BackgroundTaskResponse)
def create_health_task(service: Service):
    return service.create_health_task()


@router.get("/{task_id}", response_model=BackgroundTaskResponse)
def get_task(task_id: UUID, service: Service):
    return service.get(task_id)
