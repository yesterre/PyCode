"""Small operation-scoped retry primitive; never a business-task retry."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypeVar
from uuid import UUID

from backend.app.config import RepositoryRetrySettings


T = TypeVar("T")


def run_with_bounded_retry(
    operation: Callable[[], T],
    *,
    operation_name: str,
    project_id: UUID | None,
    settings: RepositoryRetrySettings,
    should_retry: Callable[[Exception], bool],
    sleeper: Callable[[float], None],
    jitter: Callable[[], float],
    logger: logging.Logger,
    before_retry: Callable[[int, Exception], None] | None = None,
) -> T:
    """Retry one whitelisted sub-operation without changing task state."""

    for attempt in range(1, settings.max_attempts + 1):
        try:
            return operation()
        except Exception as exc:
            if attempt >= settings.max_attempts or not should_retry(exc):
                raise
            delay = min(
                settings.max_delay_seconds,
                settings.base_delay_seconds
                * (2 ** (attempt - 1))
                * (1 + 0.25 * max(0.0, min(1.0, jitter()))),
            )
            error_code = getattr(exc, "code", type(exc).__name__)
            logger.warning(
                "Repository operation retry project_id=%s operation=%s "
                "attempt=%s next_attempt=%s error_code=%s delay=%s",
                project_id,
                operation_name,
                attempt,
                attempt + 1,
                error_code,
                delay,
            )
            if before_retry is not None:
                before_retry(attempt + 1, exc)
            sleeper(delay)
    raise AssertionError("bounded retry loop exhausted without returning or raising")
