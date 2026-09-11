"""Add explicit current Snapshot and lifecycle state.

Revision ID: 20260910_0002
Revises: 20260910_0001
Create Date: 2026-09-10
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260910_0002"
down_revision: str | None = "20260910_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("current_snapshot_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "project_snapshots",
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.alter_column(
        "project_snapshots", "file_count",
        existing_type=sa.Integer(), nullable=True,
    )
    op.alter_column(
        "project_snapshots", "node_count",
        existing_type=sa.Integer(), nullable=True,
    )
    op.alter_column(
        "project_snapshots", "edge_count",
        existing_type=sa.Integer(), nullable=True,
    )
    op.create_check_constraint(
        op.f("ck_project_snapshots_snapshot_status"),
        "project_snapshots",
        "status IN ('indexing', 'ready', 'failed')",
    )
    op.create_check_constraint(
        op.f("ck_project_snapshots_snapshot_ready_complete"),
        "project_snapshots",
        "status <> 'ready' OR ("
        "index_artifact_path IS NOT NULL AND "
        "graph_artifact_path IS NOT NULL AND "
        "file_count IS NOT NULL AND "
        "node_count IS NOT NULL AND "
        "edge_count IS NOT NULL AND "
        "error_message IS NULL)",
    )

    # Phase 3 selected the current analysis by latest-ready ordering. Preserve
    # that exact deterministic rule while introducing the explicit pointer.
    op.execute(
        sa.text(
            """
            UPDATE projects AS project
            SET current_snapshot_id = candidate.id
            FROM (
                SELECT DISTINCT ON (project_id) id, project_id
                FROM project_snapshots
                WHERE status = 'ready'
                ORDER BY project_id, updated_at DESC, id DESC
            ) AS candidate
            WHERE project.id = candidate.project_id
            """
        )
    )
    op.create_foreign_key(
        op.f("fk_projects_current_snapshot_id_project_snapshots"),
        "projects", "project_snapshots",
        ["current_snapshot_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_projects_current_snapshot_id",
        "projects", ["current_snapshot_id"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_projects_current_snapshot_id", table_name="projects")
    op.drop_constraint(
        op.f("fk_projects_current_snapshot_id_project_snapshots"),
        "projects", type_="foreignkey",
    )
    op.drop_column("projects", "current_snapshot_id")
    op.drop_constraint(
        op.f("ck_project_snapshots_snapshot_ready_complete"),
        "project_snapshots", type_="check",
    )
    op.drop_constraint(
        op.f("ck_project_snapshots_snapshot_status"),
        "project_snapshots", type_="check",
    )

    # Phase 3 required non-null counters. Failed/in-progress Phase 4 rows have
    # no completed summary, so use neutral counters only for downgrade safety.
    op.execute(
        sa.text(
            """
            UPDATE project_snapshots
            SET file_count = COALESCE(file_count, 0),
                node_count = COALESCE(node_count, 0),
                edge_count = COALESCE(edge_count, 0)
            WHERE file_count IS NULL
               OR node_count IS NULL
               OR edge_count IS NULL
            """
        )
    )
    op.alter_column(
        "project_snapshots", "edge_count",
        existing_type=sa.Integer(), nullable=False,
    )
    op.alter_column(
        "project_snapshots", "node_count",
        existing_type=sa.Integer(), nullable=False,
    )
    op.alter_column(
        "project_snapshots", "file_count",
        existing_type=sa.Integer(), nullable=False,
    )
    op.drop_column("project_snapshots", "error_message")
