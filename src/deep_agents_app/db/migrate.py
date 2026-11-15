"""Programmatic Alembic upgrade + LangGraph checkpoint table setup."""

from __future__ import annotations

from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from psycopg.rows import dict_row
from sqlalchemy import Engine, text

from ..config import Settings

# Session-level advisory lock so several replicas never migrate at the same time.
_MIGRATION_LOCK = 724_190_551


def alembic_config(engine: Engine, schema: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parent.parent / "migrations"))
    config.attributes["engine"] = engine
    config.attributes["schema"] = schema
    return config


def migrate(settings: Settings, engine: Engine) -> None:
    schema = settings.database_schema
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as lock_connection:
        lock_connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": _MIGRATION_LOCK})
        try:
            lock_connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
            command.upgrade(alembic_config(engine, schema), "head")
            setup_checkpointer(settings)
        finally:
            lock_connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _MIGRATION_LOCK})


def checkpointer_connection(settings: Settings) -> psycopg.Connection:
    """A dedicated autocommit connection, as PostgresSaver requires."""
    return psycopg.connect(
        settings.psycopg_url,
        autocommit=True,
        row_factory=dict_row,  # type: ignore[arg-type]
        options=settings.search_path_option,
    )


def setup_checkpointer(settings: Settings) -> None:
    from langgraph.checkpoint.postgres import PostgresSaver

    with checkpointer_connection(settings) as connection:
        PostgresSaver(connection).setup()  # type: ignore[arg-type]
