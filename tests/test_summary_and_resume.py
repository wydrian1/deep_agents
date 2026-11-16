import json
import threading
from datetime import timedelta
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage
from sqlalchemy import update

from deep_agents_app.agents import runner
from deep_agents_app.db.schema import workflow_runs
from deep_agents_app.runs.store import now
from deep_agents_app.summary import day_start
from deep_agents_app.workflows import diagnostics
from deep_agents_app.workflows.scheduler import resume_orphans

from .test_workflows import (
    Scripted,
    agent,
    approval_flow,
    children,
    detail,
    edge,
    node,
    save,
    spec_of,
    start,
    wait_step,
    wait_workflow,
)


def test_day_start_uses_the_configured_time_zone():
    zone = ZoneInfo("Europe/Budapest")
    moment = now().replace(year=2026, month=11, day=16, hour=23, minute=30)  # 00:30 on the 17th in Budapest
    assert day_start(moment, zone).isoformat().startswith("2026-11-17T00:00:00")
    assert day_start(moment, ZoneInfo("UTC")).isoformat().startswith("2026-11-16T00:00:00")


def test_the_summary_counts_today_and_exposes_only_content_free_causes(client, monkeypatch):
    first = client.post("/api/runs", json={"prompt": "SECRET standalone prompt", "mode": "test"}).json()["id"]
    assert client.get(f"/api/runs/{first}").status_code == 200
    from .conftest import wait_for_status

    wait_for_status(client, first)
    a, b = agent(client, "First", "Check."), agent(client, "Second", "Report.")
    spec = spec_of(
        [node("start", "start"), node("one", "agent", "One", agent=a), node("two", "agent", "Two", agent=b), node("end", "end")],
        [edge("start", "one"), edge("one", "two"), edge("two", "end")],
    )
    envelope = json.dumps({"outcome": {"status": "blocked", "reason": "source unreachable"}})
    monkeypatch.setattr(runner, "build_test_model", lambda prompt: Scripted(responses=[AIMessage(content=envelope)]))
    run_id = start(client, save(client, spec=spec).json()["id"], text="SECRET workflow input").json()["id"]
    wait_workflow(client, run_id)

    summary = client.get("/api/summary").json()
    assert summary["agents"]["total"] == 1 and summary["agents"]["by_status"] == {"completed": 1}  # workflow children are not standalone
    assert summary["workflows"]["total"] == 1 and summary["workflows"]["problem_count"] == 1
    problem = summary["workflows"]["problems"][0]
    assert problem["id"] == run_id and problem["outcome"] == "blocked" and problem["reason"] == "source unreachable"
    assert summary["tokens"]["calls"] >= 0 and summary["limit"] == {"rate_limited_calls": 0, "paused_until": None}
    assert "SECRET" not in json.dumps(summary)  # no prompts, inputs or results


def test_a_failed_workflow_without_a_child_reason_shows_its_localized_error(client):
    run_id = start(client, approval_flow(client)).json()["id"]
    wait_step(client, run_id, "gate", "approval")
    client.post(f"/api/workflow-runs/{run_id}/cancel")
    wait_workflow(client, run_id)
    problems = client.get("/api/summary").json()["workflows"]["problems"]
    assert problems == []  # a run the owner cancelled is not a problem
    run_id = start(client, approval_flow(client)).json()["id"]
    wait_step(client, run_id, "gate", "approval")
    client.post(f"/api/workflow-runs/{run_id}/approve", json={"node_id": "gate", "approved": False})
    wait_workflow(client, run_id)
    problem = client.get("/api/summary").json()["workflows"]["problems"][0]
    assert problem["reason"] == "The owner did not approve continuing the workflow."


def test_the_diagnostics_endpoint_lists_the_sanitized_reports(client):
    engine = client.app.state.engine
    run_id = "11111111-1111-1111-1111-111111111111"
    try:
        raise ValueError("SECRET message")
    except ValueError as error:
        assert diagnostics.record_failure(engine, run_id, "resume", error)
    body = client.get("/api/workflow-diagnostics").json()
    assert [r["run_id"] for r in body["reports"]] == [run_id] and body["reports"][0]["phase"] == "resume"
    assert "SECRET" not in json.dumps(body) and body["reports"][0]["body"]["exceptions"][0]["type"] == "ValueError"


def test_an_expired_lease_is_resumed_automatically_but_a_live_one_is_not(client):
    run_id = start(client, approval_flow(client)).json()["id"]
    wait_step(client, run_id, "gate", "approval")
    workflows, pool = client.app.state.workflows, client.app.state.controllers
    assert workflows.orphaned_runs() == []  # the live controller holds the lease
    assert resume_orphans(workflows, pool) == []
    pool.stop.set()  # the controller goes away
    pool.join(10)
    pool.stop = threading.Event()
    with client.app.state.engine.begin() as db:
        db.execute(update(workflow_runs).where(workflow_runs.c.id == run_id).values(lease_until=now() - timedelta(seconds=1)))
    assert workflows.orphaned_runs() == [run_id]
    client.post(f"/api/workflow-runs/{run_id}/approve", json={"node_id": "gate", "approved": True})
    assert resume_orphans(workflows, pool) == [run_id]
    assert wait_workflow(client, run_id)["status"] == "completed"
    assert len(children(client, run_id)) == 1  # finished work is never relaunched
    assert detail(client, run_id)["status"] == "completed" and workflows.orphaned_runs() == []


def test_a_run_nobody_ever_claimed_is_resumed_after_a_grace_period(client):
    a = agent(client)
    from .test_workflows import linear

    workflow_id = save(client, spec=linear(a)).json()["id"]
    workflows = client.app.state.workflows
    run_id = workflows.start(workflow_id, 1, "x", accepted_data_flow=True)  # created, controller never launched (crash)
    assert workflows.orphaned_runs() == []  # still within the grace period
    with client.app.state.engine.begin() as db:
        db.execute(update(workflow_runs).where(workflow_runs.c.id == run_id).values(created=now() - timedelta(seconds=120)))
    assert workflows.orphaned_runs() == [run_id]
    resume_orphans(workflows, client.app.state.controllers)
    assert wait_workflow(client, run_id)["status"] == "completed"


def test_auto_resume_can_be_turned_off(settings, database_url, tmp_path):
    from fastapi.testclient import TestClient

    from deep_agents_app.main import create_app

    from .conftest import make_settings

    off = make_settings(database_url, settings.database_schema, tmp_path, workflow_auto_resume=False, scheduler_enabled=True, scheduler_interval_seconds=3600)
    with TestClient(create_app(off)) as client:
        assert len(client.app.state.scheduler.extra) == 1  # only the due-schedule hook
