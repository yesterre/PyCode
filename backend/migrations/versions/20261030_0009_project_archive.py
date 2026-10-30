"""Add independent, nullable Project archive state without changing existing data.

Revision ID: 20261030_0009
Revises: 20261029_0008
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261030_0009"
down_revision: str | None = "20261029_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE projects IN ACCESS EXCLUSIVE MODE"))
    if connection.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM projects WHERE archived_at IS NOT NULL)"
    )).scalar_one():
        raise RuntimeError("Refusing to remove Project archive data. Restore archived Projects before downgrade.")
    op.drop_column("projects", "archived_at")
