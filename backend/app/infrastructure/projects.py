from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from threading import Lock
from typing import Iterator
from uuid import UUID, uuid4

from backend.app.core.errors import BackendError
from backend.app.core.models import IndexSummary, Project, ProjectStatus


class InMemoryProjectStore:
    """Thread-safe records and per-project locks, owned by one app instance.

    Records are immutable snapshots so GET can safely serialize them after
    releasing the store lock. This is not a cross-process coordination scheme.
    """

    def __init__(self) -> None:
        self._guard = Lock()
        self._projects: dict[UUID, Project] = {}
        self._operations: dict[UUID, Lock] = {}

    def create(self, name: str, repo_url: str, branch: str | None = None) -> Project:
        with self._guard:
            now = datetime.now(timezone.utc)
            project = Project(uuid4(), name, repo_url, branch, ProjectStatus.CREATED, now, now)
            self._projects[project.id] = project
            self._operations[project.id] = Lock()
            return project

    def get(self, project_id: UUID) -> Project:
        with self._guard:
            return self._get(project_id)

    def _get(self, project_id: UUID) -> Project:
        try:
            return self._projects[project_id]
        except KeyError:
            raise BackendError("project_not_found", "Project does not exist.") from None

    def set_status(
        self, project_id: UUID, status: ProjectStatus, *,
        summary: IndexSummary | None = None, error: str | None = None,
    ) -> Project:
        with self._guard:
            updated = replace(
                self._get(project_id), status=status, index_summary=summary,
                last_error=error, updated_at=datetime.now(timezone.utc),
            )
            self._projects[project_id] = updated
            return updated

    @contextmanager
    def operation(self, project_id: UUID) -> Iterator[None]:
        with self._guard:
            self._get(project_id)
            lock = self._operations[project_id]
        if not lock.acquire(blocking=False):
            raise BackendError("project_busy", "Another operation is running for this project.")
        try:
            yield
        finally:
            lock.release()
