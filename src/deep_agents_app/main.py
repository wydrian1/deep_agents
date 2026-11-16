"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy import text

from . import __version__
from .agents.supervisor import make_executor
from .api import agents as agents_api
from .api import langgraph, routes
from .api import memory as memory_api
from .api import workflows as workflows_api
from .api.limits import BodyLimitMiddleware
from .auth import oauth
from .config import Settings, get_settings
from .db.engine import make_engine
from .db.migrate import migrate
from .runs.store import RunStore
from .saved.scheduler import Scheduler
from .saved.store import SavedAgentStore
from .workflows.engine import ControllerPool
from .workflows.scheduler import run_due_workflows
from .workflows.store import WorkflowStore

log = logging.getLogger(__name__)
# The built web UI (ui/ -> npm run build copies it here; the Docker image does this for you).
UI_DIR = Path(__file__).resolve().parent / "static" / "app"
FALLBACK_PAGE = (
    "<!doctype html><meta charset=utf-8><title>Deep Agents</title>"
    "<h1>Deep Agents</h1><p>The web UI is not built in this installation. "
    "Run <code>npm run build</code> in <code>ui/</code>, or use the Docker image. "
    "The API is available: <a href=/api/docs>/api/docs</a>.</p>"
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings)
        if settings.auto_migrate:
            migrate(settings, engine)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        store = RunStore(engine, settings)
        executor = make_executor(settings, store)
        agents = SavedAgentStore(store, settings)
        workflows = WorkflowStore(store, settings)
        controllers = ControllerPool(settings, workflows, executor.submit)
        scheduler = Scheduler(
            agents, executor.submit, settings.scheduler_interval_seconds,
            extra=[lambda moment: run_due_workflows(workflows, controllers, moment)],
        )
        app.state.engine, app.state.store, app.state.executor = engine, store, executor
        app.state.agents, app.state.scheduler = agents, scheduler
        app.state.workflows, app.state.controllers = workflows, controllers
        if not settings.auth_enabled:
            log.warning("AUTH_ENABLED=false: the service has NO login or access protection.")
        if settings.scheduler_enabled:
            scheduler.start()
        yield
        scheduler.stop()
        controllers.shutdown()
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

    ui_headers = {
        "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }

    @app.get("/", include_in_schema=False)
    def index() -> Response:
        page = UI_DIR / "index.html"
        if not page.is_file():
            return Response(FALLBACK_PAGE, media_type="text/html", headers=ui_headers)
        return FileResponse(page, media_type="text/html", headers={**ui_headers, "Cache-Control": "no-cache"})

    @app.get("/assets/{name:path}", include_in_schema=False)
    def asset(name: str) -> Response:
        root = (UI_DIR / "assets").resolve()
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(404, "not found")
        # Vite emits content-hashed file names, so assets can be cached forever.
        return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"})
    app.add_middleware(BodyLimitMiddleware, language=settings.app_language)
    app.include_router(routes.router())
    app.include_router(agents_api.router())
    app.include_router(memory_api.router())
    app.include_router(workflows_api.router())
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
