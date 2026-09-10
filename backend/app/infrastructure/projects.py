from __future__ import annotations

from contextlib import contextmanager
from threading import Lock
from typing import Iterator
from uuid import UUID

from backend.app.core.errors import BackendError


class ProjectOperationLocks:
    """Preserve Phase 2 per-project exclusion without owning persistent data."""

    def __init__(self) -> None:
        self._guard = Lock()
        self._operations: dict[UUID, Lock] = {}

    @contextmanager
    def operation(self, project_id: UUID) -> Iterator[None]:
        with self._guard:
            lock = self._operations.setdefault(project_id, Lock())
        if not lock.acquire(blocking=False):
            raise BackendError("project_busy", "Another operation is running for this project.")
        try:
            yield
        finally:
            lock.release()
