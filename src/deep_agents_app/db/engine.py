from __future__ import annotations

from sqlalchemy import Engine, create_engine

from ..config import Settings


def make_engine(settings: Settings) -> Engine:
    # Tables live in the app's own schema; search_path keeps the SQL schema-agnostic.
    return create_engine(
        settings.sqlalchemy_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=10,
        connect_args={"options": settings.search_path_option},
    )
