"""Alembic environment.

Used two ways: programmatically (``db.migrate``, which passes an engine) and from the
``alembic`` CLI (``alembic -c alembic.ini upgrade head``), which builds one from settings.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import Engine

from deep_agents_app.config import get_settings
from deep_agents_app.db.engine import make_engine
from deep_agents_app.db.schema import metadata

config = context.config
engine: Engine | None = config.attributes.get("engine")
schema: str = config.attributes.get("schema", "")
if engine is None:
    settings = get_settings()
    engine, schema = make_engine(settings), settings.database_schema


def run_migrations_online() -> None:
    assert engine is not None
    with engine.begin() as connection:
        context.configure(
            connection=connection,
            target_metadata=metadata,
            version_table_schema=schema,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("Offline migrations are not supported.")
run_migrations_online()
