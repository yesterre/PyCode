import logging

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from backend.app.core.errors import BackendError, ERROR_CATALOG, error_spec
from backend.app.observability import safe_error_code


logger = logging.getLogger("pycode.backend")
# Compatibility export for callers/tests that imported the old API-level map.
ERROR_STATUS = {code: spec.http_status for code, spec in ERROR_CATALOG.items()}


def register_error_handlers(app: FastAPI) -> None:
    async def handle_validation(request: Request, exc: RequestValidationError):
        request.scope.get("state", {}).get("observation", {})["error_code"] = "request_validation"
        if not request.url.path.startswith("/api/v1/auth/"):
            return await request_validation_exception_handler(request, exc)
        # Pydantic's default error JSON includes raw input, including passwords
        # and unknown fields. Keep only bounded metadata for credential routes.
        return JSONResponse(status_code=422, content={"detail": [
            {"type": error["type"], "loc": error["loc"], "msg": "Invalid authentication request."}
            for error in exc.errors()
        ]}, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

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
        auth_request = request.url.path.startswith("/api/v1/auth/")
        observation = request.scope.get("state", {}).get("observation", {})
        observation["error_code"] = safe_error_code(code)
        if exc.__cause__ is not None or status_code >= 500 or not isinstance(exc, BackendError):
            logger.error("request_failed error_code=%s", safe_error_code(code), extra={
                "request_id": observation.get("request_id"),
                "route": getattr(request.scope.get("route"), "path", "unmatched"),
                "status": status_code, "error_code": safe_error_code(code),
            })
        headers = {"WWW-Authenticate": "Bearer"} if status_code == 401 else {}
        if observation.get("request_id"):
            headers["X-Request-ID"] = observation["request_id"]
        if auth_request:
            headers.update({"Cache-Control": "no-store", "Pragma": "no-cache"})
        return JSONResponse(status_code=status_code, content={"detail": {"code": code, "message": message}},
                            headers=headers)

    app.add_exception_handler(RequestValidationError, handle_validation)
    for error_type in (BackendError, PermissionError, FileNotFoundError,
                       NotADirectoryError, SyntaxError, UnicodeError, Exception):
        app.add_exception_handler(error_type, handle_error)
