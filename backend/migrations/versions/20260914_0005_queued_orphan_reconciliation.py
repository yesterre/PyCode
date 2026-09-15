"""Add queued task broker publish reconciliation metadata.

Revision ID: 20260914_0005
Revises: 20260914_0004
Create Date: 2026-09-14
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260914_0005"
down_revision: str | None = "20260914_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "background_tasks",
        sa.Column(
            "enqueue_attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "background_tasks",
        sa.Column("last_enqueue_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "background_tasks",
        sa.Column("enqueue_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "background_tasks",
        sa.Column("next_enqueue_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_background_tasks_background_task_enqueue_attempt_count"),
        "background_tasks",
        "enqueue_attempt_count >= 0",
    )
    op.execute(
        "UPDATE background_tasks "
        "SET next_enqueue_attempt_at = now() "
        "WHERE status = 'queued'"
    )
    op.create_index(
        "ix_background_tasks_queued_unconfirmed_next",
        "background_tasks",
        ["next_enqueue_attempt_at"],
        unique=False,
        postgresql_where=sa.text(
            "status = 'queued' AND enqueue_confirmed_at IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_background_tasks_queued_unconfirmed_next",
        table_name="background_tasks",
    )
    op.drop_constraint(
        op.f("ck_background_tasks_background_task_enqueue_attempt_count"),
        "background_tasks",
        type_="check",
    )
    op.drop_column("background_tasks", "next_enqueue_attempt_at")
    op.drop_column("background_tasks", "enqueue_confirmed_at")
    op.drop_column("background_tasks", "last_enqueue_attempt_at")
    op.drop_column("background_tasks", "enqueue_attempt_count")
