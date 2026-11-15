"""Table definitions used by the application code.

The schema itself is created and changed ONLY by Alembic migrations
(``deep_agents_app/migrations``); keep this module in sync with them.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Column, Index, Integer, MetaData, Table, Text
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
    Column("agent_id", Text),  # saved agent that started this run (manual or scheduled)
    Column("scheduled_for", TIMESTAMP(timezone=True)),  # the occurrence a scheduled run was started for
    Column("rerun_of", Text),  # the run this one is a new-instance rerun of
    Column("outcome_status", Text, nullable=False, server_default="unknown"),  # success|noop|blocked|error|unknown
    Column("event_count", Integer, nullable=False, server_default="0"),
    Column("event_bytes", BigInteger, nullable=False, server_default="0"),
    Column("created", TIMESTAMP(timezone=True), nullable=False),
    Column("updated", TIMESTAMP(timezone=True), nullable=False),
)

saved_agents = Table(
    "saved_agents",
    metadata,
    Column("id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("config", JSONB, nullable=False),  # prompt, model, mode, language, tools, files
    Column("schedule", JSONB),  # {"kind": "hourly|daily|weekly", "minute", "hour", "weekday"} or NULL
    Column("enabled", Boolean, nullable=False, server_default="false"),
    Column("next_run", TIMESTAMP(timezone=True)),
    Column("last_run_id", Text),
    Column("last_error", Text, nullable=False, server_default=""),  # message key
    Column("revision", Integer, nullable=False, server_default="1"),
    Column("created", TIMESTAMP(timezone=True), nullable=False),
    Column("updated", TIMESTAMP(timezone=True), nullable=False),
    Index("saved_agents_due", "enabled", "next_run"),
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
    Column("operation", Text, nullable=False, server_default=""),
    Column("outcome", Text, nullable=False),  # "started" until finished, then "ok" | "error"
    Column("duration_ms", Integer, nullable=False),
    Column("created", TIMESTAMP(timezone=True), nullable=False),
    Index("tool_audit_run_id_id", "run_id", "id"),
)

llm_calls = Table(
    "llm_calls",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("run_id", Text, nullable=False),
    Column("call_id", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("status", Text, nullable=False),  # "ok" | "error"
    Column("error", Text, nullable=False, server_default=""),  # message key, never raw text
    Column("scope", Text, nullable=False, server_default="main"),  # "main" | "subagent"
    Column("node", Text, nullable=False, server_default=""),
    Column("http_status", Integer),
    Column("started", TIMESTAMP(timezone=True), nullable=False),
    Column("duration_ms", Integer, nullable=False),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("cache_read_tokens", Integer),
    Column("reasoning_tokens", Integer),
    Index("llm_calls_run_id_id", "run_id", "id"),
    Index("llm_calls_started", "started"),
)

scheduler_state = Table(
    "scheduler_state",
    metadata,
    Column("id", Integer, primary_key=True),  # single row, id = 1
    Column("heartbeat", TIMESTAMP(timezone=True), nullable=False),
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
