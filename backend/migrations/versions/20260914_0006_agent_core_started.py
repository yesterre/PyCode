"""Add the durable Agent Core no-retry boundary.

Revision ID: 20260914_0006
Revises: 20260914_0005
Create Date: 2026-09-14
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260914_0006"
down_revision: str | None = "20260914_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("core_started_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "core_started_at")
