from backend.app.api.errors import ERROR_STATUS
from backend.app.core.errors import BackendError, ErrorKind, error_spec


def test_existing_public_error_statuses_remain_compatible():
    expected = {
        "project_not_found": 404,
        "repository_branch_missing": 422,
        "repository_timeout": 504,
        "repository_failed": 502,
        "repository_changed": 409,
        "project_busy": 409,
        "project_lock_unavailable": 503,
        "project_lock_lost": 503,
        "model_unavailable": 503,
        "model_timeout": 504,
        "model_failed": 502,
        "database_unavailable": 503,
        "database_conflict": 409,
        "database_failed": 500,
        "task_queue_unavailable": 503,
        "idempotency_conflict": 409,
        "repository_index_in_progress": 409,
    }
    assert {code: ERROR_STATUS[code] for code in expected} == expected


def test_error_kinds_describe_nature_without_global_retry_flag():
    assert error_spec("repository_branch_missing").kind == ErrorKind.PERMANENT
    assert error_spec("repository_failed").kind == ErrorKind.PERMANENT
    assert error_spec("model_failed").kind == ErrorKind.PERMANENT
    assert error_spec("project_busy").kind == ErrorKind.TRANSIENT
    assert error_spec("project_lock_unavailable").kind == ErrorKind.TRANSIENT
    assert error_spec("project_lock_lost").kind == ErrorKind.CONSISTENCY
    assert error_spec("repository_changed").kind == ErrorKind.CONSISTENCY
    assert error_spec("repository_timeout").kind == ErrorKind.TIMEOUT
    assert error_spec("model_timeout").kind == ErrorKind.TIMEOUT
    assert not hasattr(error_spec("project_busy"), "retryable")


def test_unknown_error_is_a_safe_permanent_500():
    unknown = error_spec("future_unknown_error")
    assert unknown.kind == ErrorKind.PERMANENT
    assert unknown.http_status == 500
    assert BackendError("future_unknown_error", "safe").kind == ErrorKind.PERMANENT


def test_timeout_and_consistency_kinds_do_not_encode_agent_retry_policy():
    for code in ("model_timeout", "project_lock_lost"):
        spec = error_spec(code)
        assert spec.kind in {ErrorKind.TIMEOUT, ErrorKind.CONSISTENCY}
        assert vars(spec) == {
            "code": code,
            "kind": spec.kind,
            "http_status": ERROR_STATUS[code],
        }
