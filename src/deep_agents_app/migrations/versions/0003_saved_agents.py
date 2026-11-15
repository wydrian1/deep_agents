"""Saved agents, schedules, run provenance, outcome status, richer call/tool metrics, scheduler heartbeat.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_TS = postgresql.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.add_column("runs", sa.Column("agent_id", sa.Text))
    op.add_column("runs", sa.Column("scheduled_for", _TS))
    op.add_column("runs", sa.Column("rerun_of", sa.Text))
    op.add_column("runs", sa.Column("outcome_status", sa.Text, nullable=False, server_default="unknown"))
    op.add_column("tool_audit", sa.Column("operation", sa.Text, nullable=False, server_default=""))
    op.add_column("llm_calls", sa.Column("scope", sa.Text, nullable=False, server_default="main"))
    op.add_column("llm_calls", sa.Column("node", sa.Text, nullable=False, server_default=""))
    op.add_column("llm_calls", sa.Column("http_status", sa.Integer))
    op.alter_column("llm_calls", "cached_tokens", new_column_name="cache_read_tokens")
    op.create_table(
        "scheduler_state",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("heartbeat", _TS, nullable=False),
        sa.CheckConstraint("id = 1"),
    )
    op.create_index("runs_agent_id_status", "runs", ["agent_id", "status"])
    op.create_table(
        "saved_agents",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("config", postgresql.JSONB, nullable=False),
        sa.Column("schedule", postgresql.JSONB),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("next_run", _TS),
        sa.Column("last_run_id", sa.Text),
        sa.Column("last_error", sa.Text, nullable=False, server_default=""),
        sa.Column("revision", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created", _TS, nullable=False),
        sa.Column("updated", _TS, nullable=False),
    )
    op.create_index("saved_agents_due", "saved_agents", ["enabled", "next_run"])


def downgrade() -> None:
    op.drop_table("scheduler_state")
    op.alter_column("llm_calls", "cache_read_tokens", new_column_name="cached_tokens")
    op.drop_column("llm_calls", "http_status")
    op.drop_column("llm_calls", "node")
    op.drop_column("llm_calls", "scope")
    op.drop_column("tool_audit", "operation")
    op.drop_column("runs", "outcome_status")
    op.drop_table("saved_agents")
    op.drop_index("runs_agent_id_status", table_name="runs")
    op.drop_column("runs", "rerun_of")
    op.drop_column("runs", "scheduled_for")
    op.drop_column("runs", "agent_id")
