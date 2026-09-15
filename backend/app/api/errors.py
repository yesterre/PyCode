import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from backend.app.core.errors import BackendError, ERROR_CATALOG, error_spec


logger = logging.getLogger("pycode.backend")
# Compatibility export for callers/tests that imported the old API-level map.
ERROR_STATUS = {code: spec.http_status for code, spec in ERROR_CATALOG.items()}


def register_error_handlers(app: FastAPI) -> None:
    async def handle_error(request: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, BackendError):
            code, message = exc.code, exc.message
            status_code = error_spec(code).http_status
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
