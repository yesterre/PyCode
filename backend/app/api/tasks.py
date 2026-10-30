from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from backend.app.api.dependencies import get_background_task_service, get_current_user
from backend.app.core.models import User
from backend.app.schemas.projects import ErrorResponse
from backend.app.schemas.tasks import BackgroundTaskResponse
from backend.app.services.background_tasks import BackgroundTaskService


Service = Annotated[BackgroundTaskService, Depends(get_background_task_service)]
CurrentUser = Annotated[User, Depends(get_current_user)]
router = APIRouter(
    prefix="/api/v1/tasks", tags=["tasks"],
    responses={code: {"model": ErrorResponse} for code in (404, 409, 500, 503)},
)


@router.get("/{task_id}", response_model=BackgroundTaskResponse, responses={401: {"model": ErrorResponse}})
def get_task(task_id: UUID, user: CurrentUser, service: Service):
    return service.get_owned(task_id, user.id)
