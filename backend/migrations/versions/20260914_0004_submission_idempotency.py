"""Add submission idempotency and active repository index protection.

Revision ID: 20260914_0004
Revises: 20260911_0003
Create Date: 2026-09-14
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260914_0004"
down_revision: str | None = "20260911_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "background_tasks",
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "background_tasks",
        sa.Column("request_fingerprint", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_background_tasks_background_task_idempotency_pair"),
        "background_tasks",
        "(idempotency_key IS NULL AND request_fingerprint IS NULL) OR "
        "(idempotency_key IS NOT NULL AND request_fingerprint IS NOT NULL)",
    )
    op.create_index(
        "uq_background_tasks_idempotency_scope",
        "background_tasks",
        ["task_type", "project_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "uq_background_tasks_active_repository_index",
        "background_tasks",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text(
            "task_type = 'repository_index' AND status IN ('queued', 'running')"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_background_tasks_active_repository_index",
        table_name="background_tasks",
        postgresql_where=sa.text(
            "task_type = 'repository_index' AND status IN ('queued', 'running')"
        ),
    )
    op.drop_index(
        "uq_background_tasks_idempotency_scope",
        table_name="background_tasks",
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.drop_constraint(
        op.f("ck_background_tasks_background_task_idempotency_pair"),
        "background_tasks",
        type_="check",
    )
    op.drop_column("background_tasks", "request_fingerprint")
    op.drop_column("background_tasks", "idempotency_key")
