from deep_agents_app import main as main_module
from deep_agents_app.api.graph_view import trace_event

from .conftest import wait_for_status


def test_graph_endpoint_returns_the_real_topology_and_normalized_events(client):
    run_id = client.post("/api/runs", json={"prompt": "hello", "mode": "test"}).json()["id"]
    wait_for_status(client, run_id)
    graph = client.get(f"/api/langgraph/threads/{run_id}/graph").json()
    ids = {n["id"] for n in graph["nodes"]}
    assert graph["coverage"] == "definition" and {"__start__", "__end__", "model"} <= ids
    assert all(e["source"] in ids and e["target"] in ids for e in graph["edges"]) and graph["edges"]
    assert graph["events"] and graph["hasMore"] is False
    assert {e["kind"] for e in graph["events"]} <= {"message", "update", "interrupt"}
    assert any(e["node"] == "model" for e in graph["events"])
    assert all(len(e["detail"]) <= 6000 and len(e["summary"]) <= 180 for e in graph["events"])

    again = client.get(f"/api/langgraph/threads/{run_id}/graph?after={graph['cursor']}").json()
    assert again["events"] == [] and again["cursor"] == graph["cursor"]  # resumes after the cursor
    partial = client.get(f"/api/langgraph/threads/{run_id}/graph?after={graph['events'][0]['id']}").json()
    assert len(partial["events"]) == len(graph["events"]) - 1


def test_graph_endpoint_validation(client):
    run_id = client.post("/api/runs", json={"prompt": "x", "mode": "test"}).json()["id"]
    wait_for_status(client, run_id)
    assert client.get(f"/api/langgraph/threads/{run_id}/graph?after=999999999").status_code == 400
    assert client.get(f"/api/langgraph/threads/{run_id}/graph?after=-1").status_code == 400
    assert client.get("/api/langgraph/threads/00000000-0000-0000-0000-000000000000/graph").status_code == 404
    assert client.get("/api/langgraph/threads/nope/graph").status_code == 404


def test_a_run_without_a_stored_topology_falls_back_to_observed_steps(client, store=None):
    run = client.app.state.store.reserve_run(prompt="p", model="m", mode="test", language="en", tools=[], files=[])
    client.app.state.store.append_event(run["id"], "updates", {"model": {"messages": []}})
    client.app.state.store.append_event(run["id"], "values", {"messages": []})  # not a step event
    graph = client.get(f"/api/langgraph/threads/{run['id']}/graph").json()
    assert graph["coverage"] == "observed" and graph["nodes"] == [] and [e["node"] for e in graph["events"]] == ["model"]


def test_trace_event_normalizes_messages_updates_and_interrupts():
    message = {"id": 7, "event": "messages|tools:1", "data": [{"content": "  hello \n world ", "type": "ai"}, {"langgraph_node": "model", "langgraph_step": 3}]}
    event = trace_event(message)
    assert event and (event["node"], event["namespace"], event["kind"], event["step"], event["summary"]) == ("model", "tools:1", "message", 3, "hello world")
    calls = trace_event({"id": 8, "event": "messages", "data": [{"content": "", "tool_calls": [{"name": "webfetch"}, {"name": "github"}]}, {"langgraph_node": "model"}]})
    assert calls and calls["summary"] == "webfetch, github"
    interrupt = trace_event({"id": 9, "event": "updates", "data": {"__interrupt__": [{"x": 1}]}})
    assert interrupt and (interrupt["node"], interrupt["kind"]) == ("tools", "interrupt")
    assert trace_event({"id": 1, "event": "values", "data": {}}) is None
    assert trace_event({"id": 2, "event": "updates", "data": {"x" * 201: {}}}) is None
    assert trace_event({"id": 3, "event": "messages", "data": [{"content": "x"}]}) is None


def test_uploaded_files_are_listed_newest_first(client):
    first = client.post("/api/files?name=a.txt", content=b"one").json()
    second = client.post("/api/files?name=b.txt", content=b"two!").json()
    listed = client.get("/api/files").json()
    assert [f["id"] for f in listed] == [second["id"], first["id"]]
    assert listed[0] == {"id": second["id"], "name": "b.txt", "size": 4}


def test_the_run_list_carries_the_prompt_for_titles(client):
    run_id = client.post("/api/runs", json={"prompt": "Title me", "mode": "test"}).json()["id"]
    wait_for_status(client, run_id)
    listed = next(r for r in client.get("/api/runs").json() if r["id"] == run_id)
    assert listed["prompt"] == "Title me" and "result" not in listed


def test_the_built_ui_is_served_with_security_headers_and_a_fallback_otherwise(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "UI_DIR", tmp_path / "not-built")  # a local build must not affect this test
    fallback = client.get("/")
    assert fallback.status_code == 200 and "not built" in fallback.text
    assert "default-src 'self'" in fallback.headers["content-security-policy"]

    monkeypatch.undo()
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><title>UI</title>", encoding="utf-8")
    (tmp_path / "assets" / "app-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    monkeypatch.setattr(main_module, "UI_DIR", tmp_path)
    page = client.get("/")
    assert page.text.startswith("<!doctype html><title>UI") and page.headers["cache-control"] == "no-cache"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    asset = client.get("/assets/app-abc123.js")
    assert asset.status_code == 200 and "immutable" in asset.headers["cache-control"]
    assert client.get("/assets/missing.js").status_code == 404
    assert client.get("/assets/../secret.txt").status_code == 404
    assert client.get("/assets/%2e%2e/secret.txt").status_code == 404
