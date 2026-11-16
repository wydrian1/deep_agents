"""Protections and features carried over from the previous implementation (see docs/DIVERGENCES.md)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from deep_agents_app.agents.supervisor import worker_environment
from deep_agents_app.db.schema import saved_agents
from deep_agents_app.main import create_app
from deep_agents_app.runs.store import now
from deep_agents_app.tools import web
from deep_agents_app.tools.registry import ToolContext, build_tools

from .conftest import make_settings


@pytest.mark.parametrize(
    ("address", "public"),
    [("100.64.0.1", False), ("198.18.0.1", False), ("224.0.0.1", False), ("240.0.0.1", False), ("192.0.2.1", False),
     ("172.31.255.255", False), ("2001:db8::1", False), ("4000::1", False), ("::ffff:93.184.216.34", False),
     ("2606:4700:4700::1111", True), ("93.184.216.34", True), ("8.8.8.8", True)],
)
def test_address_filter_is_the_stricter_of_standard_and_old_rules(address, public):
    assert web.is_public_address(address) is public


def test_a_download_has_one_overall_deadline(monkeypatch, settings):
    import httpx

    monkeypatch.setattr(web, "resolve_public", lambda host: "93.184.216.34")
    clock = iter([1000.0, 1000.0, 1021.0, 1021.0, 1021.0])  # deadline = 1020; the body arrives after it
    monkeypatch.setattr(web.time, "monotonic", lambda: next(clock, 1021.0))
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text="late"))
    with pytest.raises(web.WebUnavailable):
        web.download("https://example.com/", settings, transport=transport)


def test_worker_environment_also_drops_provider_variables(settings):
    base = {"OPENAI_API_KEY": "x", "openai_base_url": "http://evil", "ANTHROPIC_API_KEY": "y", "GOOGLE_API_KEY": "z", "NO_PROXY": "a", "PATH": "1"}
    env = worker_environment(settings, base)
    assert env["PATH"] == "1"
    assert not {k for k in env if k.upper().startswith(("OPENAI", "ANTHROPIC", "GOOGLE", "NO_PROXY"))}


def test_the_tool_audit_row_is_written_before_the_call_and_settled_after(store, settings):
    run = store.reserve_run(prompt="p", model="m", mode="test", language="en", tools=["report"], files=[])
    seen = []

    def spy(*args, **kwargs):
        seen.append(store.audit_errors(run["id"]))  # a started-but-unfinished row counts as an error
        return {"ok": True}

    ctx = ToolContext(settings, store, run["id"], "en")
    from deep_agents_app.tools.registry import audited

    assert audited(ctx, "report", spy)() == {"ok": True}
    assert seen == [1] and store.audit_errors(run["id"]) == 0
    assert store.run_metrics(run["id"])["tools"][0]["operation"] == "spy"


def test_file_search_finds_text_and_reports_coverage(client, settings):
    store = client.app.state.store
    good = client.post("/api/files?name=notes.txt", content=b"Alpha beta GAMMA delta").json()
    binary = client.post("/api/files?name=blob.bin", content=b"\x00\x01\x02binary").json()
    live = store.reserve_run(prompt="p", model="m", mode="test", language="en", tools=["files", "file_search"], files=[good["id"], binary["id"]])
    tools = {t.__name__: t for t in build_tools(["files", "file_search"], ToolContext(settings, store, live["id"], "en", [good["id"], binary["id"]]))}
    result = tools["file_search"]("gamma")
    assert [m["name"] for m in result["matches"]] == ["notes.txt"] and result["matches"][0]["offset"] == 11
    assert result["scanned"] == 1 and result["attached"] == 2 and [s["name"] for s in result["skipped"]] == ["blob.bin"]
    assert "error" in tools["file_search"]("")
    assert [m["name"] for m in tools["file_search"]("NOTES")["matches"]] == ["notes.txt"]  # file-name match


def test_requests_with_unknown_fields_are_rejected(client):
    assert client.post("/api/runs", json={"prompt": "x", "mode": "test", "surprise": 1}).status_code == 422
    assert client.post("/api/agents", json={"name": "a", "config": {"prompt": "x", "mode": "test", "extra": 1}}).status_code == 422
    assert client.post("/api/agents", json={"name": "a", "config": {"prompt": "x", "mode": "test"}, "other": 1}).status_code == 422


def test_prompt_and_body_size_limits(client):
    assert client.post("/api/runs", json={"prompt": "x" * 4001, "mode": "test"}).status_code == 422
    assert client.post("/api/runs", json={"prompt": "x" * 4000, "mode": "test"}).status_code == 201
    big = client.post("/api/runs", content=b'{"prompt": "' + b"y" * 18_000 + b'", "mode": "test"}', headers={"content-type": "application/json"})
    assert big.status_code == 413 and "too large" in big.json()["detail"]
    chunks = (b"x" * 1000 for _ in range(30))  # no content-length: counted on the stream
    assert client.post("/api/agents", content=chunks, headers={"content-type": "application/json"}).status_code == 413
    assert client.post("/api/files?name=big.bin", content=b"z" * 100_000).status_code == 201  # uploads are exempt


def test_attached_files_may_total_at_most_ten_megabytes(client):
    ids = [client.post(f"/api/files?name=f{i}.txt", content=b"a" * (2 * 1024 * 1024)).json()["id"] for i in range(6)]
    ok = client.post("/api/runs", json={"prompt": "x", "mode": "test", "tools": ["files"], "files": ids[:5]})
    assert ok.status_code == 201
    too_much = client.post("/api/runs", json={"prompt": "x", "mode": "test", "tools": ["files"], "files": ids})
    assert too_much.status_code == 422 and "10 MB" in too_much.json()["detail"]


def test_runs_are_refused_when_disk_space_is_low(database_url, tmp_path):
    from sqlalchemy import create_engine, text

    settings = make_settings(database_url, "t_disk_x7", tmp_path, min_free_disk_mb=10**9)
    try:
        with TestClient(create_app(settings)) as client:
            refused = client.post("/api/runs", json={"prompt": "x", "mode": "test"})
            assert refused.status_code == 507 and "disk" in refused.json()["detail"]
    finally:
        engine = create_engine(settings.sqlalchemy_url)
        with engine.begin() as db:
            db.execute(text('DROP SCHEMA IF EXISTS "t_disk_x7" CASCADE'))
        engine.dispose()


def test_status_shows_limits_heartbeat_and_rate_limit_pause(client):
    status = client.get("/api/status").json()
    assert status["max_concurrent_runs"] == 3 and status["run_timeout_seconds"] == 3600 and status["model"] == "gw-model"
    assert status["scheduler_heartbeat"] is None and status["limit_paused_until"] is None and status["active_runs"] == 0
    client.app.state.scheduler.tick()  # a tick writes the heartbeat even when nothing is due
    assert client.get("/api/status").json()["scheduler_heartbeat"] is not None
    client.app.state.store.record_llm_call(
        run_id="r", call_id="c", model="m", status="error", error="error_rate_limited", scope="main", node="",
        http_status=429, started=now(), duration_ms=1,
    )
    assert client.get("/api/status").json()["limit_paused_until"] is not None


def test_enable_and_pause_a_saved_schedule_without_editing_the_agent(client):
    daily = {"kind": "daily", "hour": 8, "minute": 0}
    agent = client.post("/api/agents", json={"name": "a", "config": {"prompt": "x", "mode": "test"}, "schedule": daily}).json()
    assert agent["enabled"] is False and agent["next_run"] is None
    assert client.post(f"/api/agents/{agent['id']}/enable", json={"enabled": True}).status_code == 422  # consent
    enabled = client.post(f"/api/agents/{agent['id']}/enable", json={"enabled": True, "confirm_recurring_cost": True}).json()
    assert enabled["enabled"] is True and enabled["next_run"] is not None and enabled["revision"] == 2
    store = client.app.state.store
    with store.engine.begin() as db:
        db.execute(update(saved_agents).where(saved_agents.c.id == agent["id"]).values(last_error="schedule_missed"))
    paused = client.post(f"/api/agents/{agent['id']}/enable", json={"enabled": False}).json()
    assert paused["enabled"] is False and paused["next_run"] is None and paused["last_error"] == "" and paused["revision"] == 3

    bare = client.post("/api/agents", json={"name": "b", "config": {"prompt": "x", "mode": "test"}}).json()
    no_schedule = client.post(f"/api/agents/{bare['id']}/enable", json={"enabled": True, "confirm_recurring_cost": True})
    assert no_schedule.status_code == 422
    assert client.post("/api/agents/00000000-0000-0000-0000-000000000000/enable", json={"enabled": False}).status_code == 404
    assert client.post(f"/api/agents/{agent['id']}/enable", json={"enabled": False, "x": 1}).status_code == 422


def test_run_list_carries_outcome_tool_calls_and_outputs(client, settings):
    store = client.app.state.store
    live = store.reserve_run(prompt="p", model="m", mode="test", language="en", tools=["report"], files=[])
    (report,) = build_tools(["report"], ToolContext(settings, store, live["id"], "en"))
    report("Weekly", "md", text="# hi")
    listed = next(r for r in client.get("/api/runs").json() if r["id"] == live["id"])
    assert listed["tool_calls"] == 1 and [a["title"] for a in listed["artifacts"]] == ["Weekly"]
    assert listed["metrics"]["tool_calls"] == 1 and listed["outcome_status"] == "unknown"
