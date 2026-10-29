"""Add users and nullable project ownership without assigning historical data.

Revision ID: 20261029_0008
Revises: 20260915_0007
Create Date: 2026-10-29
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20261029_0008"
down_revision: str | None = "20260915_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_name", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(1024), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("status", sa.String(32), server_default=sa.text("'active'"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("account_name", name="uq_users_account_name"),
        sa.CheckConstraint(
            "account_name COLLATE \"C\" ~ '^[a-z0-9][a-z0-9_.-]{2,63}$'",
            name=op.f("ck_users_user_account_name_normalized"),
        ),
        sa.CheckConstraint(
            "password_hash COLLATE \"C\" ~ '^[!-~]+$'",
            name=op.f("ck_users_user_password_hash"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')", name=op.f("ck_users_user_status"),
        ),
    )
    op.add_column("projects", sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_projects_owner_id_users", "projects", "users", ["owner_id"], ["id"])
    op.create_index("ix_projects_owner_id", "projects", ["owner_id"], unique=False)


def downgrade() -> None:
    # Keep the check and DDL in the same PostgreSQL transaction and prevent
    # concurrent inserts/assignments from racing the destructive schema removal.
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE users, projects IN ACCESS EXCLUSIVE MODE"))
    has_identity_data = connection.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM users) "
        "OR EXISTS (SELECT 1 FROM projects WHERE owner_id IS NOT NULL)"
    )).scalar_one()
    if has_identity_data:
        raise RuntimeError(
            "Refusing to remove user or ownership data. Restore a pre-B01 backup "
            "or explicitly archive and reconcile identity data before downgrade."
        )
    op.drop_index("ix_projects_owner_id", table_name="projects")
    op.drop_constraint("fk_projects_owner_id_users", "projects", type_="foreignkey")
    op.drop_column("projects", "owner_id")
    op.drop_table("users")
