import time
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text, update

from deep_agents_app.db.schema import saved_agents
from deep_agents_app.main import create_app
from deep_agents_app.runs.store import now

from .conftest import make_settings, wait_for_status

CONFIG = {"prompt": "weekly report", "mode": "test"}
DAILY = {"kind": "daily", "hour": 8, "minute": 30}


def new_agent(client, **extra):
    response = client.post("/api/agents", json={"name": "Reporter", "config": CONFIG, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def make_due(client, agent_id, seconds_ago=1):
    store = client.app.state.store
    with store.engine.begin() as db:
        db.execute(update(saved_agents).where(saved_agents.c.id == agent_id).values(next_run=now() - timedelta(seconds=seconds_ago)))


def test_create_get_list_update_delete(client):
    agent = new_agent(client)
    assert agent["enabled"] is False and agent["schedule"] is None and agent["next_run"] is None and agent["revision"] == 1
    assert agent["config"]["tools"] == [] and agent["config"]["language"] == "en"
    assert client.get(f"/api/agents/{agent['id']}").json()["name"] == "Reporter"
    assert [a["id"] for a in client.get("/api/agents").json()] == [agent["id"]]

    body = {"name": "Renamed", "config": {**CONFIG, "prompt": "new"}, "schedule": DAILY, "revision": 1}
    updated = client.put(f"/api/agents/{agent['id']}", json=body).json()
    assert updated["name"] == "Renamed" and updated["revision"] == 2 and updated["schedule"] == {**DAILY}
    assert updated["enabled"] is False and updated["next_run"] is None  # saved schedule, not enabled

    assert client.delete(f"/api/agents/{agent['id']}").status_code == 204
    assert client.delete(f"/api/agents/{agent['id']}").status_code == 404
    assert client.get(f"/api/agents/{agent['id']}").status_code == 404
    assert client.get("/api/agents/not-an-id").status_code == 404


def test_enabling_a_schedule_needs_a_schedule_and_explicit_consent(client):
    no_schedule = client.post("/api/agents", json={"name": "a", "config": CONFIG, "enabled": True})
    assert no_schedule.status_code == 422 and "needs a schedule" in no_schedule.json()["detail"]
    no_consent = client.post("/api/agents", json={"name": "a", "config": CONFIG, "enabled": True, "schedule": DAILY})
    assert no_consent.status_code == 422 and "confirm_recurring_cost" in no_consent.json()["detail"]
    agent = new_agent(client, enabled=True, schedule=DAILY, confirm_recurring_cost=True)
    assert agent["enabled"] is True and agent["next_run"] is not None


def test_resuming_after_a_pause_needs_consent_again(client):
    agent = new_agent(client, enabled=True, schedule=DAILY, confirm_recurring_cost=True)
    paused = client.put(f"/api/agents/{agent['id']}", json={"name": "a", "config": CONFIG, "schedule": DAILY, "enabled": False, "revision": 1}).json()
    assert paused["next_run"] is None
    resume = {"name": "a", "config": CONFIG, "schedule": DAILY, "enabled": True, "revision": 2}
    assert client.put(f"/api/agents/{agent['id']}", json=resume).status_code == 422
    assert client.put(f"/api/agents/{agent['id']}", json={**resume, "confirm_recurring_cost": True}).json()["next_run"] is not None


def test_a_stale_revision_gets_409_and_changes_nothing(client):
    agent = new_agent(client)
    body = {"name": "first", "config": CONFIG, "revision": 1}
    assert client.put(f"/api/agents/{agent['id']}", json=body).status_code == 200
    stale = client.put(f"/api/agents/{agent['id']}", json={**body, "name": "second"})
    assert stale.status_code == 409 and "changed by someone else" in stale.json()["detail"]
    assert client.get(f"/api/agents/{agent['id']}").json()["name"] == "first"


def test_the_same_validation_as_direct_runs_applies(client):
    bad_tool = client.post("/api/agents", json={"name": "a", "config": {**CONFIG, "tools": ["nope"]}})
    assert bad_tool.status_code == 422
    bad_file = client.post("/api/agents", json={"name": "a", "config": {**CONFIG, "tools": ["files"], "files": ["0" * 32]}})
    assert bad_file.status_code == 422
    assert client.post("/api/agents", json={"name": "", "config": CONFIG}).status_code == 422
    assert client.post("/api/agents", json={"name": "a", "config": CONFIG, "schedule": {"kind": "daily", "hour": 24}}).status_code == 422
    assert client.post("/api/agents", json={"name": "a", "config": {**CONFIG, "mode": "live", "model": None}}).status_code == 201  # default model set


def test_run_now_starts_a_new_run_with_provenance(client):
    agent = new_agent(client)
    first = client.post(f"/api/agents/{agent['id']}/run").json()
    second = client.post(f"/api/agents/{agent['id']}/run").json()
    assert first["id"] != second["id"] and first["agent_id"] == agent["id"] and first["scheduled_for"] is None
    run = wait_for_status(client, first["id"])
    assert run["status"] == "completed" and "Test report" in run["result"]
    assert client.get(f"/api/agents/{agent['id']}").json()["last_run_id"] == second["id"]
    assert client.post("/api/agents/00000000-0000-0000-0000-000000000000/run").status_code == 404


def test_a_due_schedule_starts_a_run_through_the_scheduler(client):
    agent = new_agent(client, enabled=True, schedule=DAILY, confirm_recurring_cost=True)
    make_due(client, agent["id"])
    (run_id,) = client.app.state.scheduler.tick()
    run = wait_for_status(client, run_id)
    assert run["status"] == "completed" and run["agent_id"] == agent["id"] and run["scheduled_for"] is not None
    after = client.get(f"/api/agents/{agent['id']}").json()
    assert after["last_run_id"] == run_id and after["next_run"] > now().isoformat()


def test_the_background_scheduler_thread_starts_due_agents_on_its_own(database_url, tmp_path):
    settings = make_settings(database_url, "t_sched_x6", tmp_path, scheduler_enabled=True, scheduler_interval_seconds=1)
    try:
        with TestClient(create_app(settings)) as client:
            agent = new_agent(client, enabled=True, schedule=DAILY, confirm_recurring_cost=True)
            make_due(client, agent["id"])
            deadline = time.monotonic() + 20
            runs = []
            while not runs and time.monotonic() < deadline:
                runs = [r for r in client.get("/api/runs").json() if r["agent_id"] == agent["id"]]
                time.sleep(0.3)
            assert runs, "scheduler thread never started the due agent"
            assert wait_for_status(client, runs[0]["id"])["status"] == "completed"
            assert len([r for r in client.get("/api/runs").json() if r["agent_id"] == agent["id"]]) == 1
    finally:
        engine = create_engine(settings.sqlalchemy_url)
        with engine.begin() as db:
            db.execute(text('DROP SCHEMA IF EXISTS "t_sched_x6" CASCADE'))
        engine.dispose()


def test_deleting_an_agent_keeps_its_runs(client):
    agent = new_agent(client)
    run_id = client.post(f"/api/agents/{agent['id']}/run").json()["id"]
    wait_for_status(client, run_id)
    assert client.delete(f"/api/agents/{agent['id']}").status_code == 204
    kept = client.get(f"/api/runs/{run_id}").json()
    assert kept["status"] == "completed" and kept["agent_id"] == agent["id"]


def test_rerun_creates_a_new_instance_and_leaves_the_old_report(client):
    original = client.post("/api/runs", json={"prompt": "again", "mode": "test", "language": "hu"}).json()
    wait_for_status(client, original["id"])
    again = client.post(f"/api/runs/{original['id']}/rerun").json()
    assert again["id"] != original["id"] and again["rerun_of"] == original["id"]
    assert again["prompt"] == "again" and again["language"] == "hu" and again["model"] == "test"
    assert wait_for_status(client, again["id"])["status"] == "completed"
    assert client.get(f"/api/runs/{original['id']}").json()["result"]  # untouched
    assert client.post("/api/runs/not-a-uuid/rerun").status_code == 404


def test_an_agent_can_be_saved_from_a_previous_run_with_its_original_settings(client):
    run = client.post("/api/runs", json={"prompt": "from history", "mode": "live", "model": "claude-alias", "language": "hu"}).json()
    client.post(f"/api/runs/{run['id']}/cancel")
    agent = client.post(f"/api/agents/from-run/{run['id']}", json={"name": "From history"}).json()
    assert agent["config"] == {
        "prompt": "from history", "model": "claude-alias", "mode": "live", "language": "hu", "tools": [], "files": [],
    }
    assert client.post("/api/agents/from-run/00000000-0000-0000-0000-000000000000", json={"name": "x"}).status_code == 404
    assert client.post(f"/api/agents/from-run/{run['id']}", json={"name": "x", "enabled": True}).status_code == 422


def test_scheduling_errors_are_shown_in_the_agent_language(client):
    agent = client.post("/api/agents", json={"name": "hu", "config": {**CONFIG, "language": "hu"}, "enabled": True, "schedule": DAILY, "confirm_recurring_cost": True}).json()
    make_due(client, agent["id"], seconds_ago=3600)
    client.app.state.scheduler.tick()
    assert "kimaradt" in client.get(f"/api/agents/{agent['id']}").json()["last_error"]
