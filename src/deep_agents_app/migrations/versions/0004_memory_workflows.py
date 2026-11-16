"""Shared knowledge base (memory) and workflows.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_TS = postgresql.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.add_column("runs", sa.Column("workflow_run_id", sa.Text))
    op.add_column("runs", sa.Column("workflow_node_id", sa.Text))
    op.create_index(
        "runs_workflow_node", "runs", ["workflow_run_id", "workflow_node_id"], unique=True,
        postgresql_where=sa.text("workflow_run_id IS NOT NULL"),
    )

    op.create_table(
        "memory_entries",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("deleted", sa.Boolean, nullable=False),
        sa.Column("updated", _TS, nullable=False),
        sa.Column("writer", sa.Text, nullable=False),
    )
    op.create_table(
        "memory_versions",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("revision", sa.Integer, primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("deleted", sa.Boolean, nullable=False),
        sa.Column("updated", _TS, nullable=False),
        sa.Column("writer", sa.Text, nullable=False),
    )
    op.create_table(
        "memory_receipts",
        sa.Column("actor", sa.Text, primary_key=True),
        sa.Column("request_id", sa.Text, primary_key=True),
        sa.Column("digest", sa.Text, nullable=False),
        sa.Column("result", postgresql.JSONB, nullable=False),
    )

    op.create_table(
        "workflows",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("deleted", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created", _TS, nullable=False),
        sa.Column("updated", _TS, nullable=False),
    )
    op.create_table(
        "workflow_versions",
        sa.Column("workflow_id", sa.Text, primary_key=True),
        sa.Column("version", sa.Integer, primary_key=True),
        sa.Column("spec", postgresql.JSONB, nullable=False),
        sa.Column("agents", postgresql.JSONB, nullable=False),
        sa.Column("created", _TS, nullable=False),
    )
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("workflow_id", sa.Text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("spec", postgresql.JSONB, nullable=False),
        sa.Column("agents", postgresql.JSONB, nullable=False),
        sa.Column("input", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("created", _TS, nullable=False),
        sa.Column("updated", _TS, nullable=False),
        sa.Column("error", sa.Text, nullable=False, server_default=""),
        sa.Column("heartbeat", _TS),
        sa.Column("lease", sa.Text),
        sa.Column("lease_until", _TS),
        sa.Column("rerun_of", sa.Text),
        sa.Column("outcome_status", sa.Text, nullable=False, server_default="unknown"),
        sa.Column("scheduled_for", _TS),
    )
    op.create_index("workflow_runs_status", "workflow_runs", ["status"])
    op.create_table(
        "workflow_steps",
        sa.Column("workflow_run_id", sa.Text, primary_key=True),
        sa.Column("node_id", sa.Text, primary_key=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("child_id", sa.Text),
        sa.Column("decision", sa.Text, nullable=False, server_default="pending"),
        sa.Column("output", sa.Text, nullable=False, server_default=""),
        sa.Column("selected", sa.Text),
        sa.Column("waits", postgresql.JSONB, nullable=False),
        sa.Column("updated", _TS, nullable=False),
    )
    op.create_index("workflow_steps_child", "workflow_steps", ["child_id"])
    op.create_table(
        "workflow_schedules",
        sa.Column("workflow_id", sa.Text, primary_key=True),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("schedule", postgresql.JSONB, nullable=False),
        sa.Column("input", sa.Text, nullable=False, server_default=""),
        sa.Column("consents", postgresql.JSONB, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("next_run", _TS),
        sa.Column("last_run_id", sa.Text),
        sa.Column("last_error", sa.Text, nullable=False, server_default=""),
        sa.Column("created", _TS, nullable=False),
        sa.Column("updated", _TS, nullable=False),
    )
    op.create_table(
        "workflow_diagnostics",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Text, nullable=False),
        sa.Column("phase", sa.Text, nullable=False),
        sa.Column("created", _TS, nullable=False),
        sa.Column("body", postgresql.JSONB, nullable=False),
    )


def downgrade() -> None:
    for table in (
        "workflow_diagnostics", "workflow_schedules", "workflow_steps", "workflow_runs", "workflow_versions",
        "workflows", "memory_receipts", "memory_versions", "memory_entries",
    ):
        op.drop_table(table)
    op.drop_index("runs_workflow_node", table_name="runs")
    op.drop_column("runs", "workflow_node_id")
    op.drop_column("runs", "workflow_run_id")