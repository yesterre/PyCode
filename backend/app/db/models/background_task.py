from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base


class BackgroundTaskORM(Base):
    __tablename__ = "background_tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')",
            name="background_task_status",
        ),
        CheckConstraint("attempt_count >= 0", name="background_task_attempt_count"),
        CheckConstraint(
            "status = 'queued' OR started_at IS NOT NULL",
            name="background_task_started",
        ),
        CheckConstraint(
            "status <> 'queued' OR (started_at IS NULL AND finished_at IS NULL)",
            name="background_task_queued_timestamps",
        ),
        CheckConstraint(
            "status <> 'running' OR (started_at IS NOT NULL AND finished_at IS NULL)",
            name="background_task_running_timestamps",
        ),
        CheckConstraint(
            "status NOT IN ('completed', 'failed') OR "
            "(started_at IS NOT NULL AND finished_at IS NOT NULL)",
            name="background_task_finished_timestamps",
        ),
        CheckConstraint(
            "status <> 'completed' OR (error_code IS NULL AND error_message IS NULL)",
            name="background_task_completed_without_error",
        ),
        CheckConstraint(
            "status <> 'failed' OR (error_code IS NOT NULL AND error_message IS NOT NULL)",
            name="background_task_failed_with_error",
        ),
        Index("ix_background_tasks_status_created", "status", "created_at"),
        Index("ix_background_tasks_project_created", "project_id", "created_at"),
        Index("ix_background_tasks_snapshot_id", "snapshot_id"),
        Index("ix_background_tasks_agent_run_id", "agent_run_id"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    project_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("projects.id"), nullable=True,
    )
    snapshot_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("project_snapshots.id"), nullable=True,
    )
    agent_run_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("agent_runs.id"), nullable=True,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0"),
    )
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
