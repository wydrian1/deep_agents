"""Saved agents API: /api/agents (CRUD, run now). Schedules are executed by the scheduler."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ..auth import current_principal
from ..i18n import message
from ..llm.gateway import ModelNotConfigured
from ..runs.store import Busy, DiskLow
from ..saved.schedule import Schedule
from ..saved.store import AgentLimit, AgentNotFound, FilesMissing, RevisionConflict, SavedAgentStore
from .routes import RunRequest, normalize_run_request, public_run

_ID = re.compile(r"^[a-f0-9-]{36}$")


class AgentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are rejected

    name: str = Field(min_length=1, max_length=100)
    config: RunRequest
    schedule: Schedule | None = None
    enabled: bool = False
    # Scheduled runs call the model again and again and consume quota: enabling needs explicit consent.
    confirm_recurring_cost: bool = False


class AgentFromRun(BaseModel):
    """Save an earlier run's original settings as an agent."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    schedule: Schedule | None = None
    enabled: bool = False
    confirm_recurring_cost: bool = False


class AgentEnable(BaseModel):
    """Turn the saved schedule on/off without editing the agent. Enabling needs the same explicit consent."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    confirm_recurring_cost: bool = False


class AgentUpdate(AgentIn):
    revision: int = Field(ge=1)  # the revision you read; a stale one gets 409 instead of overwriting


def public_agent(agent: dict[str, Any]) -> dict[str, Any]:
    config = agent["config"]
    return {
        "id": agent["id"], "name": agent["name"], "config": config, "schedule": agent["schedule"],
        "enabled": agent["enabled"], "next_run": agent["next_run"].isoformat() if agent["next_run"] else None,
        "last_run_id": agent["last_run_id"],
        "last_error": message(agent["last_error"], config["language"]) if agent["last_error"] else "",
        "revision": agent["revision"], "created": agent["created"].isoformat(), "updated": agent["updated"].isoformat(),
    }


def router() -> APIRouter:
    api = APIRouter(prefix="/api/agents", dependencies=[Depends(current_principal)], tags=["agents"])

    def store_of(request: Request) -> SavedAgentStore:
        agents: SavedAgentStore = request.app.state.agents
        return agents

    def checked_id(agent_id: str, request: Request) -> str:
        if not _ID.match(agent_id):
            raise HTTPException(404, message("agent_not_found", request.app.state.settings.app_language))
        return agent_id

    def validated(body: AgentIn, request: Request) -> dict[str, Any]:
        config = normalize_run_request(body.config, request.app.state)
        language = config["language"]
        if body.enabled and body.schedule is None:
            raise HTTPException(422, message("schedule_required", language))
        if body.enabled and not body.confirm_recurring_cost:
            raise HTTPException(422, message("schedule_consent_required", language))
        return config

    @api.get("")
    def list_agents(request: Request) -> list[dict[str, Any]]:
        return [public_agent(a) for a in store_of(request).list_agents()]

    def create_from(body: AgentIn, request: Request) -> dict[str, Any]:
        config = validated(body, request)
        try:
            agent = store_of(request).create(name=body.name.strip(), config=config, schedule=body.schedule, enabled=body.enabled)
        except AgentLimit:
            raise HTTPException(409, message("agent_limit", config["language"])) from None
        return public_agent(agent)

    @api.post("", status_code=201)
    def create_agent(body: AgentIn, request: Request) -> dict[str, Any]:
        return create_from(body, request)

    @api.post("/from-run/{run_id}", status_code=201)
    def agent_from_run(run_id: str, body: AgentFromRun, request: Request) -> dict[str, Any]:
        """Save a previous run's original settings (not the current form) as a new agent."""
        state = request.app.state
        run = state.store.get(run_id) if _ID.match(run_id) else None
        if run is None:
            raise HTTPException(404, message("run_not_found", state.settings.app_language))
        config = RunRequest(
            prompt=run["prompt"], model=None if run["mode"] == "test" else run["model"], mode=run["mode"],
            language=run["language"], tools=list(run["tools"]), files=list(run["files"]),
        )
        return create_from(AgentIn(config=config, **body.model_dump()), request)

    @api.get("/{agent_id}")
    def get_agent(agent_id: str, request: Request) -> dict[str, Any]:
        agent = store_of(request).get(checked_id(agent_id, request))
        if agent is None:
            raise HTTPException(404, message("agent_not_found", request.app.state.settings.app_language))
        return public_agent(agent)

    @api.put("/{agent_id}")
    def update_agent(agent_id: str, body: AgentUpdate, request: Request) -> dict[str, Any]:
        config = validated(body, request)
        try:
            agent = store_of(request).update(
                checked_id(agent_id, request), revision=body.revision, name=body.name.strip(), config=config,
                schedule=body.schedule, enabled=body.enabled,
            )
        except AgentNotFound:
            raise HTTPException(404, message("agent_not_found", config["language"])) from None
        except RevisionConflict:
            raise HTTPException(409, message("agent_conflict", config["language"])) from None
        return public_agent(agent)

    @api.delete("/{agent_id}", status_code=204)
    def delete_agent(agent_id: str, request: Request) -> Response:
        if not store_of(request).delete(checked_id(agent_id, request)):
            raise HTTPException(404, message("agent_not_found", request.app.state.settings.app_language))
        return Response(status_code=204)

    @api.post("/{agent_id}/enable")
    def enable_agent(agent_id: str, body: AgentEnable, request: Request) -> dict[str, Any]:
        lang = request.app.state.settings.app_language
        agent_id = checked_id(agent_id, request)
        existing = store_of(request).get(agent_id)
        if existing is None:
            raise HTTPException(404, message("agent_not_found", lang))
        language = existing["config"]["language"]
        if body.enabled and not body.confirm_recurring_cost:
            raise HTTPException(422, message("schedule_consent_required", language))
        try:
            agent = store_of(request).set_enabled(agent_id, body.enabled)
        except AgentNotFound:
            raise HTTPException(404, message("agent_not_found", lang)) from None
        except ValueError:
            raise HTTPException(422, message("schedule_required", language)) from None
        return public_agent(agent)

    @api.post("/{agent_id}/run", status_code=201)
    def run_agent(agent_id: str, request: Request) -> dict[str, Any]:
        state = request.app.state
        lang = state.settings.app_language
        try:
            run = store_of(request).start_now(checked_id(agent_id, request))
        except AgentNotFound:
            raise HTTPException(404, message("agent_not_found", lang)) from None
        except Busy:
            raise HTTPException(429, message("busy", lang, limit=state.settings.max_concurrent_runs)) from None
        except ModelNotConfigured:
            raise HTTPException(422, message("model_not_configured", lang)) from None
        except DiskLow:
            raise HTTPException(507, message("disk_low", lang)) from None
        except FilesMissing:
            raise HTTPException(422, message("file_not_found", lang)) from None
        state.executor.submit(run["id"])
        return public_run(run, full=True)

    return api
