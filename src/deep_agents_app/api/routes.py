"""Run management API: /api/runs, /api/files, /api/tools."""

from __future__ import annotations

import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import storage
from ..auth import current_principal
from ..i18n import message, resolve_language
from ..llm.gateway import ModelNotConfigured, resolve_model_name
from ..runs.store import FINISHED, Busy
from ..tools.registry import catalog, validate_selection

_RUN_ID = re.compile(r"^[a-f0-9-]{36}$")


class RunRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=20000)
    model: str | None = Field(default=None, max_length=200)
    mode: Literal["live", "test"] = "live"
    language: str | None = Field(default=None, max_length=10)
    tools: list[str] = Field(default_factory=list, max_length=20)
    files: list[str] = Field(default_factory=list, max_length=storage.MAX_FILES_PER_RUN)


def public_error(run: dict[str, Any]) -> str:
    """Runs store a message key, never raw exception text; render it in the run's language."""
    return message(run["error"], run["language"]) if run["error"] else ""


def public_run(run: dict[str, Any], full: bool = False) -> dict[str, Any]:
    data = {
        "id": run["id"], "status": run["status"], "mode": run["mode"], "model": run["model"],
        "tools": run["tools"], "steps": run["steps"], "error": public_error(run),
        "created": run["created"].isoformat(), "updated": run["updated"].isoformat(),
    }
    if full:
        data |= {"prompt": run["prompt"], "result": run["result"], "language": run["language"], "files": run["files"]}
    return data


def valid_run_id(run_id: str, language: str = "en") -> str:
    if not _RUN_ID.match(run_id):
        raise HTTPException(404, message("run_not_found", language))
    return run_id


def router() -> APIRouter:
    api = APIRouter(prefix="/api", dependencies=[Depends(current_principal)], tags=["runs"])

    @api.get("/tools")
    def tools() -> list[dict[str, Any]]:
        return catalog()

    @api.post("/files", status_code=201)
    async def upload(request: Request, name: str) -> dict[str, Any]:
        """Upload one file as the raw request body (``?name=report.pdf``). Max 2 MB."""
        settings, lang = request.app.state.settings, resolve_language(None, request.app.state.settings.app_language)
        safe_name = re.sub(r"[^\w.\- ]", "_", name.replace("\\", "/").split("/")[-1])[:120].strip() or "file"
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > storage.MAX_UPLOAD_BYTES:
                raise HTTPException(413, message("upload_too_large", lang, limit=storage.MAX_UPLOAD_BYTES))
        if not body:
            raise HTTPException(400, message("upload_empty", lang))
        file_id = storage.new_id()
        storage.write_exclusive(storage.input_path(settings, file_id), bytes(body))
        return request.app.state.store.add_input_file(file_id, safe_name, len(body), storage.sha256(bytes(body)))

    @api.post("/runs", status_code=201)
    def create_run(body: RunRequest, request: Request) -> dict[str, Any]:
        state = request.app.state
        settings, store = state.settings, state.store
        language = resolve_language(body.language, settings.app_language)
        if body.mode == "test" and not settings.allow_test_mode:
            raise HTTPException(403, message("test_mode_disabled", language))
        names = list(dict.fromkeys(body.tools))
        problems = validate_selection(names)
        if problems:
            raise HTTPException(422, message("unknown_tool", language, name=", ".join(problems)))
        files = list(dict.fromkeys(body.files))
        if files and (
            "files" not in names or not all(storage.is_id(f) for f in files) or len(store.get_input_files(files)) != len(files)
        ):
            raise HTTPException(422, message("file_not_found", language))
        try:
            model = "test" if body.mode == "test" else resolve_model_name(settings, body.model)
        except ModelNotConfigured:
            raise HTTPException(422, message("model_not_configured", language)) from None
        try:
            run = store.reserve_run(
                prompt=body.prompt, model=model, mode=body.mode, language=language, tools=names, files=files
            )
        except Busy:
            raise HTTPException(429, message("busy", language, limit=settings.max_concurrent_runs)) from None
        state.executor.submit(run["id"])
        return public_run(run, full=True)

    @api.get("/runs")
    def list_runs(request: Request) -> list[dict[str, Any]]:
        return [public_run({**r, "language": request.app.state.settings.app_language}) for r in request.app.state.store.list_runs()]

    @api.get("/runs/{run_id}")
    def get_run(run_id: str, request: Request) -> dict[str, Any]:
        store = request.app.state.store
        run = store.get(valid_run_id(run_id))
        if not run:
            raise HTTPException(404, message("run_not_found", request.app.state.settings.app_language))
        return public_run(run, full=True) | {"artifacts": store.list_artifacts(run_id)}

    @api.post("/runs/{run_id}/cancel", status_code=202)
    def cancel_run(run_id: str, request: Request) -> dict[str, Any]:
        store = request.app.state.store
        run = store.get(valid_run_id(run_id))
        if not run:
            raise HTTPException(404, message("run_not_found", request.app.state.settings.app_language))
        return {"cancelled": run["status"] not in FINISHED and store.cancel(run_id)}

    @api.get("/runs/{run_id}/artifacts/{artifact_id}")
    def download_artifact(run_id: str, artifact_id: str, request: Request) -> FileResponse:
        settings, store = request.app.state.settings, request.app.state.store
        lang = settings.app_language
        artifact = store.get_artifact(artifact_id) if storage.is_id(artifact_id) else None
        if not artifact or artifact["run_id"] != valid_run_id(run_id):
            raise HTTPException(404, message("artifact_not_found", lang))
        path = storage.artifact_path(settings, run_id, artifact_id)
        try:
            storage.read_checked(path, artifact["size"], artifact["sha256"], 2 * 1024 * 1024)
        except (ValueError, OSError):
            raise HTTPException(404, message("artifact_not_found", lang)) from None
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=artifact["name"],
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox",
            },
        )

    return api
