"""Content-free model-call metrics: timing, status and token counts per call.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Text, nullable=False),
        sa.Column("call_id", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("error", sa.Text, nullable=False, server_default=""),
        sa.Column("started", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Integer, nullable=False),
        sa.Column("input_tokens", sa.Integer),
        sa.Column("output_tokens", sa.Integer),
        sa.Column("cached_tokens", sa.Integer),
        sa.Column("reasoning_tokens", sa.Integer),
    )
    op.create_index("llm_calls_run_id_id", "llm_calls", ["run_id", "id"])
    op.create_index("llm_calls_started", "llm_calls", ["started"])


def downgrade() -> None:
    op.drop_table("llm_calls")
