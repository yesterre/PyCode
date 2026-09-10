from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base


class ProjectSnapshotORM(Base):
    __tablename__ = "project_snapshots"
    __table_args__ = (
        UniqueConstraint("project_id", "commit_sha", name="uq_project_snapshots_project_commit"),
        CheckConstraint("file_count >= 0", name="snapshot_file_count"),
        CheckConstraint("node_count >= 0", name="snapshot_node_count"),
        CheckConstraint("edge_count >= 0", name="snapshot_edge_count"),
        Index("ix_project_snapshots_project_updated", "project_id", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("projects.id"), nullable=False,
    )
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    index_artifact_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    graph_artifact_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    node_count: Mapped[int] = mapped_column(Integer, nullable=False)
    edge_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
