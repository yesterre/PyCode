from fastapi import Request

from backend.app.services.projects import ProjectService


def get_project_service(request: Request) -> ProjectService:
    return request.app.state.project_service
