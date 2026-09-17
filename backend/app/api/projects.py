from typing import Annotated, Literal
from uuid import UUID
from pathlib import PurePosixPath, PureWindowsPath

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.app.api.dependencies import get_background_task_service, get_project_service
from backend.app.api.idempotency import IdempotencyKeyHeader
from backend.app.schemas.projects import (
    AnalysisResponse, AskRequest, CreateProjectRequest, ErrorResponse,
    ImpactRequest, ProjectResponse, ProjectListResponse,
)
from backend.app.schemas.snapshots import SnapshotListResponse, SnapshotResponse
from backend.app.schemas.graph import GraphResponse
from backend.app.schemas.tasks import RepositoryIndexTaskResponse
from backend.app.services.background_tasks import BackgroundTaskService
from backend.app.services.projects import ProjectService


Service = Annotated[ProjectService, Depends(get_project_service)]
TaskService = Annotated[BackgroundTaskService, Depends(get_background_task_service)]
router = APIRouter(
    prefix="/api/v1/projects", tags=["projects"],
    responses={code: {"model": ErrorResponse} for code in (403, 404, 409, 500, 502, 503, 504)},
)


@router.get("", response_model=ProjectListResponse)
def list_projects(service: Service, limit: Annotated[int, Query(ge=1, le=100)] = 50,
                  offset: Annotated[int, Query(ge=0)] = 0):
    projects, has_more = service.list_page(limit, offset)
    return ProjectListResponse(projects=[ProjectResponse.model_validate(p) for p in projects],
                               has_more=has_more)


@router.post("", status_code=201, response_model=ProjectResponse)
def create_project(body: CreateProjectRequest, service: Service):
    return service.create(body.name, body.repo_url, body.branch)


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(project_id: UUID, service: Service):
    return service.get(project_id)


@router.get("/{project_id}/snapshots", response_model=SnapshotListResponse)
def list_snapshots(project_id: UUID, service: Service,
                   limit: Annotated[int, Query(ge=1, le=100)] = 20):
    return SnapshotListResponse(snapshots=[SnapshotResponse.model_validate(snapshot)
        for snapshot in service.list_snapshots(project_id, limit)])


@router.get("/{project_id}/snapshots/{snapshot_id}", response_model=SnapshotResponse)
def get_snapshot(project_id: UUID, snapshot_id: UUID, service: Service):
    return service.get_snapshot(project_id, snapshot_id)


@router.get("/{project_id}/snapshots/{snapshot_id}/graph", response_model=GraphResponse)
def get_graph(project_id: UUID, snapshot_id: UUID, service: Service,
              q: Annotated[str | None, Query(max_length=200)] = None,
              file_path: Annotated[str | None, Query(max_length=512)] = None,
              node_type: Literal["file", "class", "function", "method"] | None = None,
              limit: Annotated[int, Query(ge=1, le=200)] = 100):
    q = q.strip() if q is not None else None
    file_path = file_path.strip() if file_path is not None else None
    if bool(q) == bool(file_path):
        raise HTTPException(status_code=422, detail=[{
            "type": "value_error", "loc": ["query", "q"],
            "msg": "Exactly one of q or file_path is required.",
        }])
    if file_path is not None:
        candidate = file_path.replace("\\", "/")
        if (PurePosixPath(candidate).is_absolute() or PureWindowsPath(file_path).is_absolute()
                or ":" in candidate or any(part in {"", ".", ".."}
                                            for part in candidate.split("/"))):
            raise HTTPException(status_code=422, detail=[{
                "type": "value_error", "loc": ["query", "file_path"],
                "msg": "file_path must be a repository-relative file path.",
            }])
        file_path = candidate
    return service.get_graph(project_id, snapshot_id, q=q, node_type=node_type,
                             file_path=file_path, limit=limit)


@router.post(
    "/{project_id}/index", status_code=202,
    response_model=RepositoryIndexTaskResponse,
)
def index_project(
    project_id: UUID, service: TaskService,
    idempotency_key: IdempotencyKeyHeader = None,
):
    task = service.create_repository_index_task(project_id, idempotency_key)
    return RepositoryIndexTaskResponse(
        task_id=task.id, project_id=project_id, status=task.status,
    )


@router.post("/{project_id}/ask", response_model=AnalysisResponse)
def ask_project(project_id: UUID, body: AskRequest, service: Service):
    return service.ask(project_id, body.question, body.model)


@router.post("/{project_id}/impact", response_model=AnalysisResponse)
def impact_project(project_id: UUID, body: ImpactRequest, service: Service):
    return service.impact(project_id, body.file_path, body.model)
