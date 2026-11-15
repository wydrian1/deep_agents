"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import resources

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text

from . import __version__
from .agents.runner import RunExecutor
from .api import langgraph, routes
from .auth import oauth
from .config import Settings, get_settings
from .db.engine import make_engine
from .db.migrate import migrate
from .runs.store import RunStore

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings)
        if settings.auto_migrate:
            migrate(settings, engine)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        store = RunStore(engine, settings)
        executor = RunExecutor(settings, store)
        app.state.engine, app.state.store, app.state.executor = engine, store, executor
        if not settings.auth_enabled:
            log.warning("AUTH_ENABLED=false: the service has NO login or access protection.")
        yield
        executor.shutdown()
        engine.dispose()

    app = FastAPI(title="Deep Agents", version=__version__, lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.settings = settings

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> JSONResponse:
        try:
            with app.state.engine.connect() as db:
                db.execute(text("SELECT 1"))
        except Exception:
            return JSONResponse({"status": "unavailable", "database": False}, status_code=503)
        return JSONResponse({"status": "ok", "database": True, "auth": settings.auth_enabled})

    @app.get("/", include_in_schema=False)
    def index() -> Response:
        page = resources.files(__package__).joinpath("static", "index.html").read_text(encoding="utf-8")
        return Response(
            page,
            media_type="text/html",
            headers={"Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'", "X-Content-Type-Options": "nosniff"},
        )

    app.include_router(routes.router())
    app.include_router(langgraph.router())
    if settings.auth_enabled:
        app.include_router(oauth.router(settings))
    return app


def __getattr__(name: str) -> FastAPI:
    # ``uvicorn deep_agents_app.main:app`` builds the app lazily, so importing this
    # module (tests, tooling) never requires a configured environment.
    if name == "app":
        return create_app()
    raise AttributeError(name)
