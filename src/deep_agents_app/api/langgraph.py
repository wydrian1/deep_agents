"""Narrow LangGraph-SDK-compatible read API: one thread per run (thread id == run id).

Implements thread state and the SSE join stream, plus cancel. It is NOT the full LangGraph
Platform server: no arbitrary graph input, checkpoint forking or config from the client.
A UI built on ``@langchain/langgraph-sdk`` / assistant-ui can target these paths.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth import current_principal
from ..i18n import message
from ..runs.store import ACTIVE, RunStore
from .graph_view import studio_graph
from .routes import public_error

_UUID = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
MAX_CONNECTIONS = 8
STREAM_SECONDS = 65 * 60
_open_streams = 0
NO_STORE = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


def thread_state(store: RunStore, run_id: str) -> dict[str, Any]:
    run = store.get(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    messages = run["messages"] or [{"type": "human", "id": run_id + "-prompt", "content": run["prompt"]}]
    if not run["messages"] and run["result"]:
        messages.append({"type": "ai", "id": run_id + "-result", "content": run["result"]})
    return {
        "values": {"messages": messages},
        "next": [],
        "tasks": [],
        "checkpoint": {"thread_id": run_id, "checkpoint_ns": "", "checkpoint_id": None},
        "metadata": {"stream_cursor": store.last_event_id(run_id), "status": run["status"], "error": public_error(run)},
        "created_at": run["updated"].isoformat(),
        "parent_checkpoint": None,
    }


def sse(event: str, data: Any, event_id: int | None = None) -> str:
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return f"{prefix}event: {event}\ndata: {json.dumps(data)}\n\n"


async def stream_events(request: Request, run_id: str, cursor: int) -> AsyncIterator[str]:
    global _open_streams
    store: RunStore = request.app.state.store
    lang = request.app.state.settings.app_language
    _open_streams += 1
    until = time.monotonic() + STREAM_SECONDS
    try:
        while time.monotonic() < until and not await request.is_disconnected():
            events = await run_in_threadpool(store.events_after, run_id, cursor, 64)
            for item in events:
                yield sse(item["event"], item["data"], item["id"])
                cursor = item["id"]
            state = await run_in_threadpool(thread_state, store, run_id)
            if len(events) < 64 and cursor >= state["metadata"]["stream_cursor"] and state["metadata"]["status"] != ACTIVE:
                yield sse("values", state["values"])
                if state["metadata"]["status"] == "failed":
                    yield sse("error", {"message": state["metadata"]["error"] or message("run_failed", lang)})
                yield sse("end", {})
                return
            if not events:
                yield ": keepalive\n\n"
            await asyncio.sleep(0 if len(events) == 64 else 0.5)
    except Exception:
        yield sse("error", {"message": message("stream_failed", lang)})
    finally:
        _open_streams -= 1


def router() -> APIRouter:
    api = APIRouter(prefix="/api/langgraph", dependencies=[Depends(current_principal)], tags=["langgraph"])

    def checked(run_id: str, request: Request) -> str:
        if not _UUID.match(run_id) or not request.app.state.store.get(run_id):
            raise HTTPException(404, "run not found")
        return run_id

    @api.get("/threads/{run_id}/state")
    def state(run_id: str, request: Request) -> JSONResponse:
        checked(run_id, request)
        return JSONResponse(thread_state(request.app.state.store, run_id), headers=NO_STORE)

    @api.get("/threads/{run_id}/graph")
    def graph(run_id: str, request: Request, after: int = 0) -> JSONResponse:
        """The run's real LangGraph topology and normalized step events after a cursor (for the Studio view)."""
        checked(run_id, request)
        try:
            result = studio_graph(request.app.state.store, run_id, after)
        except ValueError:
            raise HTTPException(400, "invalid event cursor") from None
        return JSONResponse(result, headers=NO_STORE)

    @api.get("/threads/{run_id}/runs/{same_id}/stream")
    async def join(run_id: str, same_id: str, request: Request) -> StreamingResponse:
        checked(run_id, request)
        lang = request.app.state.settings.app_language
        cursor_text = request.headers.get("last-event-id", "0")
        if same_id != run_id or not cursor_text.isdigit() or len(cursor_text) > 12:
            raise HTTPException(400, "invalid stream request")
        cursor = int(cursor_text)
        if cursor > await run_in_threadpool(request.app.state.store.last_event_id, run_id):
            raise HTTPException(400, "invalid event cursor")
        if _open_streams >= MAX_CONNECTIONS:
            raise HTTPException(429, message("stream_limit", lang))
        return StreamingResponse(
            stream_events(request, run_id, cursor),
            media_type="text/event-stream",
            headers={**NO_STORE, "X-Accel-Buffering": "no", "x-thread-id": run_id, "x-run-id": run_id},
        )

    @api.post("/threads/{run_id}/runs/{same_id}/cancel")
    def cancel(run_id: str, same_id: str, request: Request) -> JSONResponse:
        checked(run_id, request)
        if same_id != run_id:
            raise HTTPException(400, "invalid cancel request")
        request.app.state.store.cancel(run_id)
        return JSONResponse(None, headers=NO_STORE)

    return api
