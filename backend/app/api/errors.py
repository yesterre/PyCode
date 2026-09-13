import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from backend.app.core.errors import BackendError


logger = logging.getLogger("pycode.backend")
ERROR_STATUS = {
    "invalid_path": 422,
    "project_not_found": 404,
    "invalid_repository": 422,
    "repository_forbidden": 403,
    "unsafe_repository": 403,
    "workspace_unavailable": 409,
    "git_unavailable": 503,
    "repository_timeout": 504,
    "repository_failed": 502,
    "repository_branch_missing": 422,
    "repository_commit_unavailable": 409,
    "repository_changed": 409,
    "project_busy": 409,
    "project_not_ready": 409,
    "artifacts_unavailable": 409,
    "artifact_publish_failed": 500,
    "snapshot_not_found": 404,
    "path_forbidden": 403,
    "model_unavailable": 503,
    "model_timeout": 504,
    "model_failed": 502,
    "database_unavailable": 503,
    "database_conflict": 409,
    "database_failed": 500,
    "agent_run_not_found": 404,
    "background_task_not_found": 404,
    "background_task_state_conflict": 409,
    "task_queue_unavailable": 503,
}


def register_error_handlers(app: FastAPI) -> None:
    async def handle_error(request: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, BackendError):
            code, message = exc.code, exc.message
            status_code = ERROR_STATUS.get(code, 500)
        elif isinstance(exc, PermissionError):
            status_code, code, message = 403, "permission_denied", "Project file access was denied."
        elif isinstance(exc, (FileNotFoundError, NotADirectoryError)):
            status_code, code, message = 404, "path_not_found", "Repository or target path does not exist."
        elif isinstance(exc, (SyntaxError, UnicodeError)):
            status_code, code, message = 422, "invalid_source", "Python source could not be parsed or decoded."
        else:
            status_code, code, message = 500, "internal_error", "An unexpected server error occurred."
        if exc.__cause__ is not None or status_code >= 500 or not isinstance(exc, BackendError):
            logger.error("Request failed: %s %s (%s)", request.method, request.url.path, code,
                         exc_info=(type(exc), exc, exc.__traceback__))
        return JSONResponse(status_code=status_code, content={"detail": {"code": code, "message": message}})

    for error_type in (BackendError, PermissionError, FileNotFoundError,
                       NotADirectoryError, SyntaxError, UnicodeError, Exception):
        app.add_exception_handler(error_type, handle_error)
