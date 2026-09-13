"""Add persistent background task execution state.

Revision ID: 20260911_0003
Revises: 20260910_0002
Create Date: 2026-09-11
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260911_0003"
down_revision: str | None = "20260910_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "background_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("agent_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempt_count >= 0", name=op.f("ck_background_tasks_background_task_attempt_count")),
        sa.CheckConstraint(
            "status = 'queued' OR started_at IS NOT NULL",
            name=op.f("ck_background_tasks_background_task_started"),
        ),
        sa.CheckConstraint(
            "status <> 'queued' OR (started_at IS NULL AND finished_at IS NULL)",
            name=op.f("ck_background_tasks_background_task_queued_timestamps"),
        ),
        sa.CheckConstraint(
            "status <> 'running' OR (started_at IS NOT NULL AND finished_at IS NULL)",
            name=op.f("ck_background_tasks_background_task_running_timestamps"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('completed', 'failed') OR "
            "(started_at IS NOT NULL AND finished_at IS NOT NULL)",
            name=op.f("ck_background_tasks_background_task_finished_timestamps"),
        ),
        sa.CheckConstraint(
            "status <> 'completed' OR (error_code IS NULL AND error_message IS NULL)",
            name=op.f("ck_background_tasks_background_task_completed_without_error"),
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR (error_code IS NOT NULL AND error_message IS NOT NULL)",
            name=op.f("ck_background_tasks_background_task_failed_with_error"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')",
            name=op.f("ck_background_tasks_background_task_status"),
        ),
        sa.ForeignKeyConstraint(
            ["agent_run_id"], ["agent_runs.id"],
            name=op.f("fk_background_tasks_agent_run_id_agent_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"],
            name=op.f("fk_background_tasks_project_id_projects"),
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["project_snapshots.id"],
            name=op.f("fk_background_tasks_snapshot_id_project_snapshots"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_background_tasks")),
    )
    op.create_index(
        "ix_background_tasks_agent_run_id", "background_tasks", ["agent_run_id"], unique=False,
    )
    op.create_index(
        "ix_background_tasks_project_created", "background_tasks",
        ["project_id", "created_at"], unique=False,
    )
    op.create_index(
        "ix_background_tasks_snapshot_id", "background_tasks", ["snapshot_id"], unique=False,
    )
    op.create_index(
        "ix_background_tasks_status_created", "background_tasks",
        ["status", "created_at"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_background_tasks_status_created", table_name="background_tasks")
    op.drop_index("ix_background_tasks_snapshot_id", table_name="background_tasks")
    op.drop_index("ix_background_tasks_project_created", table_name="background_tasks")
    op.drop_index("ix_background_tasks_agent_run_id", table_name="background_tasks")
    op.drop_table("background_tasks")
