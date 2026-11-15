"""Table definitions used by the application code.

The schema itself is created and changed ONLY by Alembic migrations
(``deep_agents_app/migrations``); keep this module in sync with them.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, Index, Integer, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP

metadata = MetaData()

runs = Table(
    "runs",
    metadata,
    Column("id", Text, primary_key=True),
    Column("prompt", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("mode", Text, nullable=False),
    Column("language", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("tools", JSONB, nullable=False),
    Column("files", JSONB, nullable=False),
    Column("result", Text, nullable=False, server_default=""),
    Column("error", Text, nullable=False, server_default=""),
    Column("steps", Integer, nullable=False, server_default="0"),
    Column("messages", JSONB, nullable=False),
    Column("graph", JSONB),
    Column("event_count", Integer, nullable=False, server_default="0"),
    Column("event_bytes", BigInteger, nullable=False, server_default="0"),
    Column("created", TIMESTAMP(timezone=True), nullable=False),
    Column("updated", TIMESTAMP(timezone=True), nullable=False),
)

run_events = Table(
    "run_events",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("run_id", Text, nullable=False),
    Column("event", Text, nullable=False),
    Column("data", JSONB, nullable=False),
    Column("created", TIMESTAMP(timezone=True), nullable=False),
    Index("run_events_run_id_id", "run_id", "id"),
)

tool_audit = Table(
    "tool_audit",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("run_id", Text, nullable=False),
    Column("tool", Text, nullable=False),
    Column("outcome", Text, nullable=False),
    Column("duration_ms", Integer, nullable=False),
    Column("created", TIMESTAMP(timezone=True), nullable=False),
    Index("tool_audit_run_id_id", "run_id", "id"),
)

input_files = Table(
    "input_files",
    metadata,
    Column("id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("size", Integer, nullable=False),
    Column("sha256", Text, nullable=False),
    Column("created", TIMESTAMP(timezone=True), nullable=False),
)

artifacts = Table(
    "artifacts",
    metadata,
    Column("id", Text, primary_key=True),
    Column("run_id", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("size", Integer, nullable=False),
    Column("sha256", Text, nullable=False),
    Column("created", TIMESTAMP(timezone=True), nullable=False),
    Index("artifacts_run_id_created", "run_id", "created"),
)
