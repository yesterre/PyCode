from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base


class AgentRunORM(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')",
            name="agent_run_status",
        ),
        CheckConstraint("prompt_tokens IS NULL OR prompt_tokens >= 0", name="agent_run_prompt_tokens"),
        CheckConstraint(
            "completion_tokens IS NULL OR completion_tokens >= 0",
            name="agent_run_completion_tokens",
        ),
        Index("ix_agent_runs_project_created", "project_id", "created_at"),
        Index("ix_agent_runs_snapshot_id", "snapshot_id"),
        *(CheckConstraint(f"{name} IS NULL OR (jsonb_typeof({name}) = 'object' AND octet_length({name}::text) <= {limit})",
                          name=f"agent_run_{name}_size")
          for name,limit in [('request_metadata',2048),('execution_metadata',2048),('result_metadata',16384)]),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("projects.id"), nullable=False,
    )
    snapshot_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("project_snapshots.id"), nullable=True,
    )
    task: Mapped[str] = mapped_column(Text, nullable=False)
    run_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_metadata: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    execution_metadata: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    result_metadata: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    core_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
