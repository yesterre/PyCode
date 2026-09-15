"""Add durable worker execution ownership and lease metadata.

Revision ID: 20260915_0007
Revises: 20260914_0006
Create Date: 2026-09-15
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260915_0007"
down_revision: str | None = "20260914_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "background_tasks",
        sa.Column("execution_token", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "background_tasks",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "background_tasks",
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "background_tasks",
        sa.Column("worker_id", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "ix_background_tasks_running_lease",
        "background_tasks",
        ["lease_expires_at", "id"],
        unique=False,
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("ix_background_tasks_running_lease", table_name="background_tasks")
    op.drop_column("background_tasks", "worker_id")
    op.drop_column("background_tasks", "last_heartbeat_at")
    op.drop_column("background_tasks", "lease_expires_at")
    op.drop_column("background_tasks", "execution_token")
