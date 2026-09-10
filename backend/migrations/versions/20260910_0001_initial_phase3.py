"""Create Phase 3 persistent business tables.

Revision ID: 20260910_0001
Revises: None
Create Date: 2026-09-10
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260910_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("repo_url", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("workspace_path", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('created', 'preparing', 'indexing', 'ready', 'failed')",
            name=op.f("ck_projects_project_status"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_projects"),
    )
    op.create_table(
        "project_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("commit_sha", sa.String(length=64), nullable=False),
        sa.Column("index_artifact_path", sa.Text(), nullable=True),
        sa.Column("graph_artifact_path", sa.Text(), nullable=True),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("node_count", sa.Integer(), nullable=False),
        sa.Column("edge_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("edge_count >= 0", name=op.f("ck_project_snapshots_snapshot_edge_count")),
        sa.CheckConstraint("file_count >= 0", name=op.f("ck_project_snapshots_snapshot_file_count")),
        sa.CheckConstraint("node_count >= 0", name=op.f("ck_project_snapshots_snapshot_node_count")),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name="fk_project_snapshots_project_id_projects",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_project_snapshots"),
        sa.UniqueConstraint("project_id", "commit_sha", name="uq_project_snapshots_project_commit"),
    )
    op.create_index(
        "ix_project_snapshots_project_updated",
        "project_snapshots",
        ["project_id", "updated_at"],
        unique=False,
    )
    op.create_table(
        "agent_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("run_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "completion_tokens IS NULL OR completion_tokens >= 0",
            name=op.f("ck_agent_runs_agent_run_completion_tokens"),
        ),
        sa.CheckConstraint(
            "prompt_tokens IS NULL OR prompt_tokens >= 0",
            name=op.f("ck_agent_runs_agent_run_prompt_tokens"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')",
            name=op.f("ck_agent_runs_agent_run_status"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name="fk_agent_runs_project_id_projects",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["project_snapshots.id"],
            name="fk_agent_runs_snapshot_id_project_snapshots",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_runs"),
    )
    op.create_index(
        "ix_agent_runs_project_created", "agent_runs", ["project_id", "created_at"], unique=False,
    )
    op.create_index("ix_agent_runs_snapshot_id", "agent_runs", ["snapshot_id"], unique=False)
    op.create_table(
        "trace_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("tool_name", sa.String(length=255), nullable=True),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("sequence >= 0", name=op.f("ck_trace_events_trace_event_sequence")),
        sa.ForeignKeyConstraint(
            ["run_id"], ["agent_runs.id"], name="fk_trace_events_run_id_agent_runs",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_trace_events"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_trace_events_run_sequence"),
    )


def downgrade() -> None:
    op.drop_table("trace_events")
    op.drop_index("ix_agent_runs_snapshot_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_project_created", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_index("ix_project_snapshots_project_updated", table_name="project_snapshots")
    op.drop_table("project_snapshots")
    op.drop_table("projects")
