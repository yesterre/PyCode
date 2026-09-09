from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from backend.app.api.dependencies import get_project_service
from backend.app.schemas.projects import (
    AnalysisResponse, AskRequest, CreateProjectRequest, ErrorResponse,
    ImpactRequest, ProjectResponse,
)
from backend.app.services.projects import ProjectService


Service = Annotated[ProjectService, Depends(get_project_service)]
router = APIRouter(
    prefix="/api/v1/projects", tags=["projects"],
    responses={code: {"model": ErrorResponse} for code in (403, 404, 409, 500, 502, 503, 504)},
)


@router.post("", status_code=201, response_model=ProjectResponse)
def create_project(body: CreateProjectRequest, service: Service):
    return service.create(body.name, body.repo_url, body.branch)


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(project_id: UUID, service: Service):
    return service.get(project_id)


@router.post("/{project_id}/index", response_model=ProjectResponse)
def index_project(project_id: UUID, service: Service):
    return service.index(project_id)


@router.post("/{project_id}/ask", response_model=AnalysisResponse)
def ask_project(project_id: UUID, body: AskRequest, service: Service):
    return service.ask(project_id, body.question, body.model)


@router.post("/{project_id}/impact", response_model=AnalysisResponse)
def impact_project(project_id: UUID, body: ImpactRequest, service: Service):
    return service.impact(project_id, body.file_path, body.model)
