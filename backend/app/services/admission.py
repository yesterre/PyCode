"""Shared admission rules for expensive user work; task state remains in SQL."""
from uuid import UUID

from backend.app.config import AdmissionSettings
from backend.app.core.errors import BackendError
from backend.app.infrastructure.redis import RedisRateLimiter
from backend.app.repositories import UnitOfWork


class AdmissionControl:
    def __init__(self, settings: AdmissionSettings, limiter: RedisRateLimiter) -> None:
        self.settings, self.limiter = settings, limiter

    def admit_task(self, uow: UnitOfWork, owner_id: UUID, operation: str) -> None:
        # The caller has inserted its new task in this transaction. Include that
        # row in the count and roll the entire submission back on rejection.
        if uow.background_tasks.lock_owner_and_count_active(owner_id) > self.settings.active_tasks:
            raise BackendError("task_quota_exceeded", "Active background task quota exceeded.")
        limit = {"index": self.settings.index_requests, "agent": self.settings.agent_requests}[operation]
        self.limiter.check(owner_id, operation, limit, self.settings.window_seconds)

    def admit_analysis(self, owner_id: UUID) -> None:
        self.limiter.check(owner_id, "analysis", self.settings.analysis_requests, self.settings.window_seconds)
