"""Workflow API: /api/workflows (definitions, versions, start, schedule) and /api/workflow-runs."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ..auth import current_principal
from ..i18n import message
from ..runs.store import DiskLow
from ..saved.schedule import Schedule
from ..workflows.diagnostics import list_reports
from ..workflows.engine import ControllerPool
from ..workflows.spec import WorkflowRejected, valid_uuid
from ..workflows.store import (
    MAX_ACTIVE_WORKFLOWS,
    MAX_AGENT_PROMPT,
    MAX_STORED_RUNS,
    WorkflowConflict,
    WorkflowStore,
)

_NODE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
NOT_FOUND = {"wf_not_found", "wf_run_not_found"}


class SaveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    name: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=0)
    spec: dict[str, Any]


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    input: str = ""
    accepted_data_flow: bool = False


class RerunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted_data_flow: bool = False


class ScheduleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schedule: Schedule
    version: int = Field(ge=1)
    input: str = ""
    accepted_schedule: bool = False
    accepted_data_flow: bool = False


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(pattern=_NODE.pattern)
    approved: bool


def localized_error(value: str, language: str) -> str:
    """Errors are stored as message keys, optionally ``key:cause`` (a failed child's own cause)."""
    if not value:
        return ""
    head, _, cause = value.partition(":")
    return message(head, language) + (": " + message(cause, language) if cause else "")


def router() -> APIRouter:
    api = APIRouter(prefix="/api", dependencies=[Depends(current_principal)], tags=["workflows"])

    def store_of(request: Request) -> WorkflowStore:
        store: WorkflowStore = request.app.state.workflows
        return store

    def pool_of(request: Request) -> ControllerPool:
        pool: ControllerPool = request.app.state.controllers
        return pool

    def language(request: Request) -> str:
        return str(request.app.state.settings.app_language)

    def problem(request: Request, error: WorkflowRejected) -> HTTPException:
        values = dict(error.values)
        if error.key == "wf_prompt_long" or error.key == "wf_agent_in_workflow_prompt":
            values.setdefault("limit", MAX_AGENT_PROMPT)
        return HTTPException(404 if error.key in NOT_FOUND else 422, message(error.key, language(request), **values))

    def public_run(run: dict[str, Any], request: Request) -> dict[str, Any]:
        return {**run, "error": localized_error(run["error"], language(request))}

    def checked(workflow_id: str, request: Request) -> str:
        try:
            return valid_uuid(workflow_id)
        except WorkflowRejected as error:
            raise problem(request, error) from None

    @api.get("/workflows")
    def overview(request: Request) -> dict[str, Any]:
        store = store_of(request)
        definitions = store.definitions()
        for item in definitions:  # schedule problems are stored as message keys
            if item["schedule"]:
                item["schedule"]["last_error"] = localized_error(item["schedule"]["last_error"], language(request))
        return {
            "engine": "langgraph", "definitions": definitions,
            "runs": [public_run(r, request) for r in store.run_summaries()],
            "max_nodes": 12, "max_agent_nodes": 8, "max_active_workflows": MAX_ACTIVE_WORKFLOWS, "max_stored_runs": MAX_STORED_RUNS,
            "max_agent_prompt": MAX_AGENT_PROMPT, "timeout_seconds": request.app.state.settings.run_timeout_seconds,
        }

    @api.get("/workflow-diagnostics")
    def diagnostics(request: Request) -> dict[str, Any]:
        """Content-free controller failure reports (error classes and code locations only), newest first."""
        return {"reports": list_reports(store_of(request).engine)}

    @api.post("/workflows")
    def save(body: SaveBody, request: Request) -> dict[str, Any]:
        """Save a NEW immutable version (the server freezes the saved agents' current configuration)."""
        try:
            return store_of(request).save(
                name=body.name, spec_input=body.spec, expected_version=body.expected_version,
                workflow_id=valid_uuid(body.id) if body.id else None,
            )
        except WorkflowRejected as error:
            raise problem(request, error) from None
        except WorkflowConflict:
            raise HTTPException(409, message("wf_conflict", language(request))) from None

    @api.get("/workflows/{workflow_id}/versions")
    def versions(workflow_id: str, request: Request) -> dict[str, Any]:
        found = store_of(request).versions(checked(workflow_id, request))
        if found is None:
            raise HTTPException(404, message("wf_not_found", language(request)))
        return {"versions": found}

    @api.delete("/workflows/{workflow_id}", status_code=204)
    def delete(workflow_id: str, request: Request) -> Response:
        if not store_of(request).delete(checked(workflow_id, request)):
            raise HTTPException(404, message("wf_not_found", language(request)))
        return Response(status_code=204)

    @api.post("/workflows/{workflow_id}/start", status_code=201)
    def start(workflow_id: str, body: StartBody, request: Request) -> dict[str, Any]:
        try:
            run_id = store_of(request).start(checked(workflow_id, request), body.version, body.input, accepted_data_flow=body.accepted_data_flow)
        except WorkflowRejected as error:
            raise problem(request, error) from None
        except DiskLow:
            raise HTTPException(507, message("disk_low", language(request))) from None
        pool_of(request).launch(run_id)
        return {"id": run_id}

    @api.put("/workflows/{workflow_id}/schedule")
    def set_schedule(workflow_id: str, body: ScheduleBody, request: Request) -> dict[str, Any]:
        try:
            first = store_of(request).set_schedule(
                checked(workflow_id, request), body.schedule, body.version, body.input,
                accepted_schedule=body.accepted_schedule, accepted_data_flow=body.accepted_data_flow,
            )
        except WorkflowRejected as error:
            raise problem(request, error) from None
        return {"ok": True, "next_run": first.isoformat()}

    @api.delete("/workflows/{workflow_id}/schedule", status_code=204)
    def remove_schedule(workflow_id: str, request: Request) -> Response:
        store_of(request).remove_schedule(checked(workflow_id, request))
        return Response(status_code=204)

    @api.post("/workflows/{workflow_id}/schedule/pause")
    def pause_schedule(workflow_id: str, request: Request) -> dict[str, Any]:
        try:
            store_of(request).pause_schedule(checked(workflow_id, request))
        except WorkflowRejected as error:
            raise problem(request, error) from None
        return {"ok": True}

    @api.get("/workflow-runs/{run_id}")
    def run_detail(run_id: str, request: Request) -> dict[str, Any]:
        detail = store_of(request).run_detail(checked(run_id, request))
        if detail is None:
            raise HTTPException(404, message("wf_run_not_found", language(request)))
        return public_run(detail, request)

    @api.post("/workflow-runs/{run_id}/rerun", status_code=201)
    def rerun(run_id: str, body: RerunBody, request: Request) -> dict[str, Any]:
        """A new instance of the original version, input and frozen agents (new model usage)."""
        try:
            new_id = store_of(request).start("", 1, "", accepted_data_flow=body.accepted_data_flow, rerun_of=checked(run_id, request))
        except WorkflowRejected as error:
            raise problem(request, error) from None
        except DiskLow:
            raise HTTPException(507, message("disk_low", language(request))) from None
        pool_of(request).launch(new_id)
        return {"id": new_id}

    @api.post("/workflow-runs/{run_id}/cancel", status_code=202)
    def cancel(run_id: str, request: Request) -> dict[str, Any]:
        store_of(request).fail_run(checked(run_id, request), "wf_err_owner_cancel", status="cancelled")
        return {"ok": True}

    @api.post("/workflow-runs/{run_id}/continue", status_code=202)
    def continue_run(run_id: str, request: Request) -> dict[str, Any]:
        """Relaunch the controller after its lease expired (same checkpoint and children; never a new workflow)."""
        try:
            store_of(request).can_continue(checked(run_id, request))
        except WorkflowRejected as error:
            raise problem(request, error) from None
        pool_of(request).launch(run_id)
        return {"ok": True}

    @api.post("/workflow-runs/{run_id}/approve")
    def approve(run_id: str, body: ApproveBody, request: Request) -> dict[str, Any]:
        """The owner's decision for an approval step. It grants no shell or tool permission."""
        try:
            store_of(request).approve(checked(run_id, request), body.node_id, body.approved)
        except WorkflowRejected as error:
            raise problem(request, error) from None
        return {"ok": True}

    return api
