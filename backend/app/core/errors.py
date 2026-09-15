from dataclasses import dataclass
from enum import StrEnum


class ErrorKind(StrEnum):
    """Describe an error's nature, not whether an operation may retry it."""

    PERMANENT = "permanent"
    TRANSIENT = "transient"
    CONSISTENCY = "consistency"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class ErrorSpec:
    code: str
    kind: ErrorKind
    http_status: int


def _spec(code: str, kind: ErrorKind, status: int) -> ErrorSpec:
    return ErrorSpec(code=code, kind=kind, http_status=status)


ERROR_CATALOG: dict[str, ErrorSpec] = {
    # Request and durable resource errors.
    code: _spec(code, ErrorKind.PERMANENT, status)
    for code, status in {
        "invalid_path": 422,
        "project_not_found": 404,
        "invalid_repository": 422,
        "repository_forbidden": 403,
        "unsafe_repository": 403,
        "repository_failed": 502,
        "repository_branch_missing": 422,
        "snapshot_not_found": 404,
        "path_forbidden": 403,
        "agent_run_not_found": 404,
        "background_task_not_found": 404,
        "model_failed": 502,
        "idempotency_conflict": 409,
        "repository_index_in_progress": 409,
    }.items()
}
ERROR_CATALOG.update({
    code: _spec(code, ErrorKind.TRANSIENT, status)
    for code, status in {
        "workspace_unavailable": 409,
        "git_unavailable": 503,
        "project_busy": 409,
        "project_lock_unavailable": 503,
        "project_not_ready": 409,
        "artifacts_unavailable": 409,
        "model_unavailable": 503,
        "database_unavailable": 503,
        "task_queue_unavailable": 503,
    }.items()
})
ERROR_CATALOG.update({
    code: _spec(code, ErrorKind.CONSISTENCY, status)
    for code, status in {
        "repository_commit_unavailable": 409,
        "repository_changed": 409,
        "project_lock_lost": 503,
        "artifact_publish_failed": 500,
        "database_conflict": 409,
        "database_failed": 500,
        "agent_run_state_conflict": 409,
        "background_task_state_conflict": 409,
        "background_task_consistency_error": 500,
    }.items()
})
ERROR_CATALOG.update({
    code: _spec(code, ErrorKind.TIMEOUT, status)
    for code, status in {
        "repository_timeout": 504,
        "model_timeout": 504,
    }.items()
})


UNKNOWN_ERROR_SPEC = _spec("internal_error", ErrorKind.PERMANENT, 500)


def error_spec(code: str) -> ErrorSpec:
    """Return a safe classification; unknown errors never imply retry."""

    return ERROR_CATALOG.get(code, ErrorSpec(code, ErrorKind.PERMANENT, 500))


class BackendError(Exception):
    """An application error with a public message, independent of HTTP."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def kind(self) -> ErrorKind:
        return error_spec(self.code).kind
