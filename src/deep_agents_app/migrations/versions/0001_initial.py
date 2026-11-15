"""Initial schema: runs, run events, tool audit, input files, artifacts.

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_TS = postgresql.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("prompt", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("mode", sa.Text, nullable=False),
        sa.Column("language", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("tools", postgresql.JSONB, nullable=False),
        sa.Column("files", postgresql.JSONB, nullable=False),
        sa.Column("result", sa.Text, nullable=False, server_default=""),
        sa.Column("error", sa.Text, nullable=False, server_default=""),
        sa.Column("steps", sa.Integer, nullable=False, server_default="0"),
        sa.Column("messages", postgresql.JSONB, nullable=False),
        sa.Column("graph", postgresql.JSONB),
        sa.Column("event_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("event_bytes", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("created", _TS, nullable=False),
        sa.Column("updated", _TS, nullable=False),
    )
    op.create_index("runs_status_created", "runs", ["status", "created"])
    op.create_table(
        "run_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Text, nullable=False),
        sa.Column("event", sa.Text, nullable=False),
        sa.Column("data", postgresql.JSONB, nullable=False),
        sa.Column("created", _TS, nullable=False),
    )
    op.create_index("run_events_run_id_id", "run_events", ["run_id", "id"])
    op.create_table(
        "tool_audit",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Text, nullable=False),
        sa.Column("tool", sa.Text, nullable=False),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("duration_ms", sa.Integer, nullable=False),
        sa.Column("created", _TS, nullable=False),
    )
    op.create_index("tool_audit_run_id_id", "tool_audit", ["run_id", "id"])
    op.create_table(
        "input_files",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("size", sa.Integer, nullable=False),
        sa.Column("sha256", sa.Text, nullable=False),
        sa.Column("created", _TS, nullable=False),
    )
    op.create_table(
        "artifacts",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("run_id", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("size", sa.Integer, nullable=False),
        sa.Column("sha256", sa.Text, nullable=False),
        sa.Column("created", _TS, nullable=False),
    )
    op.create_index("artifacts_run_id_created", "artifacts", ["run_id", "created"])


def downgrade() -> None:
    for table in ("artifacts", "input_files", "tool_audit", "run_events", "runs"):
        op.drop_table(table)
