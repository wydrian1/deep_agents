"""Run management API: /api/runs, /api/files, /api/tools."""

from __future__ import annotations

import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import storage
from ..auth import current_principal
from ..i18n import message, resolve_language
from ..llm.gateway import ModelNotConfigured
from ..runs.store import FINISHED, Busy, DiskLow
from ..saved.store import start_values
from ..tools.registry import catalog, validate_selection

_RUN_ID = re.compile(r"^[a-f0-9-]{36}$")


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are rejected, as in the previous system

    prompt: str = Field(min_length=1, max_length=20000)
    model: str | None = Field(default=None, max_length=200)
    mode: Literal["live", "test"] = "live"
    language: str | None = Field(default=None, max_length=10)
    tools: list[str] = Field(default_factory=list, max_length=20)
    files: list[str] = Field(default_factory=list, max_length=storage.MAX_FILES_PER_RUN)


def normalize_run_request(body: RunRequest, state: Any) -> dict[str, Any]:
    """Validate a run request and return the stored configuration (model left unresolved).

    Shared by direct runs and saved agents. Raises HTTPException with a localized message.
    """
    settings, store = state.settings, state.store
    language = resolve_language(body.language, settings.app_language)
    if body.mode == "test" and not settings.allow_test_mode:
        raise HTTPException(403, message("test_mode_disabled", language))
    names = list(dict.fromkeys(body.tools))
    problems = validate_selection(names)
    if problems:
        raise HTTPException(422, message("unknown_tool", language, name=", ".join(problems)))
    prompt = body.prompt.strip()
    if not prompt or len(prompt) > settings.max_prompt_chars:
        raise HTTPException(422, message("prompt_too_long", language, limit=settings.max_prompt_chars))
    files = list(dict.fromkeys(body.files))
    if files:
        found = store.get_input_files(files) if "files" in names and all(storage.is_id(f) for f in files) else []
        if len(found) != len(files):
            raise HTTPException(422, message("file_not_found", language))
        if sum(f["size"] for f in found) > storage.MAX_TOTAL_INPUT_BYTES:
            raise HTTPException(422, message("files_total_too_large", language))
    config = {
        "prompt": prompt, "model": (body.model or "").strip() or None, "mode": body.mode,
        "language": language, "tools": names, "files": files,
    }
    try:
        start_values(settings, config)  # fail now, not at the first scheduled start
    except ModelNotConfigured:
        raise HTTPException(422, message("model_not_configured", language)) from None
    return config


def public_error(run: dict[str, Any]) -> str:
    """Runs store a message key, never raw exception text; render it in the run's language."""
    return message(run["error"], run["language"]) if run["error"] else ""


def public_run(run: dict[str, Any], full: bool = False) -> dict[str, Any]:
    data = {
        "id": run["id"], "status": run["status"], "mode": run["mode"], "model": run["model"],
        "tools": run["tools"], "steps": run["steps"], "error": public_error(run), "agent_id": run.get("agent_id"), "rerun_of": run.get("rerun_of"),
        "scheduled_for": run["scheduled_for"].isoformat() if run.get("scheduled_for") else None,
        "created": run["created"].isoformat(), "updated": run["updated"].isoformat(),
        "outcome_status": run.get("outcome_status", "unknown"),
    }
    for key in ("metrics", "tool_calls", "artifacts", "workflow"):  # present in listings and in the detail view
        if key in run:
            data[key] = run[key]
    data["prompt"] = run["prompt"]  # the UI titles runs by their prompt
    if full:
        data |= {"result": run["result"], "language": run["language"], "files": run["files"]}
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

    @api.get("/files")
    def list_files(request: Request) -> list[dict[str, Any]]:
        """The most recent uploaded files (id, name, size), for attaching to a run."""
        return list(request.app.state.store.list_input_files())

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
        config = normalize_run_request(body, state)
        try:
            run = state.store.reserve_run(**start_values(state.settings, config))
        except Busy:
            raise HTTPException(429, message("busy", config["language"], limit=state.settings.max_concurrent_runs)) from None
        except DiskLow:
            raise HTTPException(507, message("disk_low", config["language"])) from None
        state.executor.submit(run["id"])
        return public_run(run, full=True)

    @api.get("/runs")
    def list_runs(request: Request) -> list[dict[str, Any]]:
        """The active and most recent runs with metrics summary, tool-call count, outputs and workflow origin."""
        rows = request.app.state.store.list_runs()
        origins = request.app.state.workflows.origins([r["id"] for r in rows if r.get("workflow_run_id")])
        return [public_run({**r, "workflow": origins.get(r["id"])}) for r in rows]

    @api.get("/runs/{run_id}")
    def get_run(run_id: str, request: Request) -> dict[str, Any]:
        store = request.app.state.store
        run = store.get(valid_run_id(run_id))
        if not run:
            raise HTTPException(404, message("run_not_found", request.app.state.settings.app_language))
        summary = store.summarize([run]).get(run_id)
        origin = request.app.state.workflows.origins([run_id]).get(run_id)
        extra = {"artifacts": store.list_artifacts(run_id), "metrics": summary, "tool_calls": summary["tool_calls"] if summary else 0, "workflow": origin}
        return public_run(run | extra, full=True)

    @api.get("/status")
    def status(request: Request) -> dict[str, Any]:
        """Service status: limits, scheduler heartbeat and an active rate-limit pause."""
        state = request.app.state
        settings = state.settings
        snapshot = state.agents.scheduler_status()
        return {
            "model": settings.default_model or None, "language": settings.app_language,
            "max_concurrent_runs": settings.max_concurrent_runs, "run_timeout_seconds": settings.run_timeout_seconds,
            "max_prompt_chars": settings.max_prompt_chars,
            "active_runs": state.store.active_count(), "auth_enabled": settings.auth_enabled,
            "scheduler_enabled": settings.scheduler_enabled, "schedule_timezone": settings.schedule_timezone,
            "scheduler_heartbeat": snapshot["heartbeat"].isoformat() if snapshot["heartbeat"] else None,
            "limit_paused_until": snapshot["paused_until"].isoformat() if snapshot["paused_until"] else None,
        }

    @api.post("/runs/{run_id}/rerun", status_code=201)
    def rerun(run_id: str, request: Request) -> dict[str, Any]:
        """Start a NEW run with the original run's settings (new id; the old report and thread are untouched)."""
        state = request.app.state
        run = state.store.get(valid_run_id(run_id))
        if not run:
            raise HTTPException(404, message("run_not_found", state.settings.app_language))
        language = run["language"]
        if run["mode"] == "test" and not state.settings.allow_test_mode:
            raise HTTPException(403, message("test_mode_disabled", language))
        problems = validate_selection(list(run["tools"]))
        if problems:
            raise HTTPException(422, message("unknown_tool", language, name=", ".join(problems)))
        files = list(run["files"])
        if files and len(state.store.get_input_files(files)) != len(files):
            raise HTTPException(422, message("file_not_found", language))
        try:
            new = state.store.reserve_run(
                prompt=run["prompt"], model=run["model"], mode=run["mode"], language=language,
                tools=list(run["tools"]), files=files, rerun_of=run_id,
            )
        except Busy:
            raise HTTPException(429, message("busy", language, limit=state.settings.max_concurrent_runs)) from None
        except DiskLow:
            raise HTTPException(507, message("disk_low", language)) from None
        state.executor.submit(new["id"])
        return public_run(new, full=True)

    @api.get("/runs/{run_id}/metrics")
    def run_metrics(run_id: str, request: Request) -> dict[str, Any]:
        """Content-free metrics for one run: summary plus per-call and per-tool detail (timing, status, tokens)."""
        store = request.app.state.store
        run = store.get(valid_run_id(run_id))
        metrics = store.run_metrics(run_id) if run else None
        if run is None or metrics is None:
            raise HTTPException(404, message("run_not_found", request.app.state.settings.app_language))
        for call in metrics["calls"]:
            call["error"] = message(call["error"], run["language"]) if call["error"] else ""
        return metrics

    @api.get("/metrics/summary")
    def metrics_summary(request: Request, days: int = Query(default=7, ge=1, le=90)) -> dict[str, Any]:
        """Per-model call counts, errors, tokens and latency over the last ``days`` days."""
        return {"days": days, "models": request.app.state.store.metrics_summary(days)}

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
