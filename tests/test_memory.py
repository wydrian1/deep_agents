import pytest

from deep_agents_app.memory import store as memory_module
from deep_agents_app.memory.store import MemoryConflict, MemoryRejected, MemoryStore
from deep_agents_app.tools.registry import ToolContext, build_tools, validate_selection

from .conftest import wait_for_status


@pytest.fixture
def memory(store):
    return MemoryStore(store.engine)


def put(memory, key="client-acme", value="hello", expected=0, request_id="r1", actor="owner", **kw):
    return memory.put(key, value, expected, actor, request_id, **kw)


def test_create_update_history_and_soft_delete_restore(memory):
    first = put(memory, expected=0)
    assert first["revision"] == 1 and first["deleted"] is False and first["writer"] == "owner"
    second = put(memory, value="v2", expected=1, request_id="r2")
    assert second["revision"] == 2
    assert memory.get("client-acme")["entry"]["value"] == "v2"
    assert memory.get("client-acme", revision=1)["entry"]["value"] == "hello"
    assert memory.get("missing")["entry"] is None

    gone = memory.delete("client-acme", 2, "owner", "r3")
    assert gone["deleted"] is True and gone["revision"] == 3
    assert memory.get("client-acme")["entry"]["deleted"] is True
    assert memory.list_entries()["entries"] == []  # hidden by default
    assert [e["key"] for e in memory.list_entries(include_deleted=True)["entries"]] == ["client-acme"]
    restored = put(memory, value="back", expected=3, request_id="r4")  # restoring is a NEW revision, not a rewrite
    assert restored["revision"] == 4 and memory.get("client-acme")["entry"]["deleted"] is False

    versions = memory.history("client-acme")["versions"]
    assert [v["revision"] for v in versions] == [4, 3, 2, 1] and [v["value"] for v in versions][-1] == "hello"
    assert memory.history("client-acme", before=3)["versions"][0]["revision"] == 2


def test_stale_revisions_conflict_and_never_overwrite(memory):
    put(memory)
    with pytest.raises(MemoryConflict) as error:
        put(memory, value="x", expected=0, request_id="r2")
    assert error.value.revision == 1
    with pytest.raises(MemoryConflict):
        put(memory, value="x", expected=5, request_id="r3")
    assert memory.get("client-acme")["entry"]["value"] == "hello"
    with pytest.raises(MemoryConflict):
        memory.delete("client-acme", 9, "owner", "r4")


def test_validation_of_names_values_and_revisions(memory):
    for bad in ("", "Upper", "-lead", "a b", "x" * 81, "ünï", "../etc"):
        with pytest.raises(MemoryRejected) as error:
            put(memory, key=bad)
        assert error.value.key == "memory_bad_key"
    for bad in ("x" * 12001, "tab\x00null", "bell\x07", "€" * 11000):  # too long, control chars, over 32 KB in bytes
        with pytest.raises(MemoryRejected) as error:
            put(memory, value=bad)
        assert error.value.key == "memory_bad_value"
    assert put(memory, value="line\nbreak\ttab", request_id="ok")["revision"] == 1  # newline/tab are fine
    with pytest.raises(MemoryRejected):
        put(memory, key="other", expected=-1)
    with pytest.raises(MemoryRejected):
        memory.delete("client-acme", 0, "owner", "d0")  # delete needs a real revision
    with pytest.raises(MemoryRejected) as error:
        put(memory, key="other", request_id="")
    assert error.value.key == "memory_bad_request_id"
    with pytest.raises(MemoryRejected):
        memory.list_entries(prefix="A%")  # literal lowercase prefixes only
    with pytest.raises(MemoryRejected):
        memory.list_entries(limit=51)


def test_replayed_requests_are_idempotent_and_id_reuse_is_an_error(memory):
    first = put(memory, request_id="same")
    again = put(memory, request_id="same")  # same id, same content: the original result, no second revision
    assert again == first and memory.history("client-acme")["versions"][0]["revision"] == 1
    assert len(memory.history("client-acme")["versions"]) == 1
    with pytest.raises(MemoryRejected) as error:
        put(memory, value="other content", request_id="same")
    assert error.value.key == "memory_request_id_reused"
    # Receipts are per actor.
    assert memory.put("other-key", "x", 0, "agent:1", "same")["writer"] == "agent:1"


def test_list_is_literal_prefix_paged_and_never_returns_values(memory):
    for index in range(7):
        put(memory, key=f"proj.{index}", value=f"secret {index}", request_id=f"p{index}")
    put(memory, key="projx", request_id="px")
    put(memory, key="other", request_id="po")
    page = memory.list_entries(prefix="proj.", limit=3)
    assert [e["key"] for e in page["entries"]] == ["proj.0", "proj.1", "proj.2"] and page["next"] == "proj.2"
    assert "value" not in page["entries"][0] and page["entries"][0]["characters"] == len("secret 0")
    rest = memory.list_entries(prefix="proj.", after=page["next"], limit=10)
    assert [e["key"] for e in rest["entries"]] == [f"proj.{i}" for i in range(3, 7)] and rest["next"] is None
    assert [e["key"] for e in memory.list_entries(prefix="proj")["entries"]][:2] == ["proj.0", "proj.1"]
    assert memory.list_entries(prefix="zzz")["entries"] == []


def test_limits_do_not_purge_silently(memory, monkeypatch):
    monkeypatch.setattr(memory_module, "MAX_KEYS", 2)
    put(memory, key="a1", request_id="1")
    put(memory, key="a2", request_id="2")
    with pytest.raises(MemoryRejected) as error:
        put(memory, key="a3", request_id="3")
    assert error.value.key == "memory_keys_full"
    memory.delete("a1", 1, "owner", "4")  # deleted names still count
    with pytest.raises(MemoryRejected):
        put(memory, key="a3", request_id="5")
    monkeypatch.setattr(memory_module, "MAX_KEYS", 200)
    monkeypatch.setattr(memory_module, "MAX_VERSIONS", 3)
    with pytest.raises(MemoryRejected) as error:
        put(memory, key="a4", request_id="6")
    assert error.value.key == "memory_history_full"
    assert memory.get("a1")["entry"]["revision"] == 2 and len(memory.history("a1")["versions"]) == 2  # nothing deleted


def test_an_agent_can_only_write_while_its_run_is_active(store, memory):
    run = store.reserve_run(prompt="p", model="m", mode="test", language="en", tools=[], files=[])
    assert put(memory, key="k1", actor="agent:" + run["id"], run_id=run["id"])["writer"].startswith("agent:")
    store.cancel(run["id"])
    with pytest.raises(MemoryRejected) as error:
        put(memory, key="k2", request_id="r9", actor="agent:" + run["id"], run_id=run["id"])
    assert error.value.key == "memory_inactive"


def test_write_grant_requires_read_grant():
    assert validate_selection(["memory_write"]) == ["memory_read"]
    assert validate_selection(["memory_read", "memory_write"]) == []


def tools_for(settings, store, run, names):
    return {t.__name__: t for t in build_tools(names, ToolContext(settings, store, run["id"], "en"))}


def test_agent_tools_read_write_conflict_and_replay(settings, store, memory):
    run = store.reserve_run(prompt="p", model="m", mode="test", language="en", tools=["memory_read", "memory_write"], files=[])
    tools = tools_for(settings, store, run, ["memory_read", "memory_write"])
    assert set(tools) == {"memory_list", "memory_get", "memory_put", "memory_delete"}
    saved = tools["memory_put"]("notes.today", "remember this", 0)
    assert saved["revision"] == 1 and saved["writer"] == "agent:" + run["id"]
    assert tools["memory_put"]("notes.today", "remember this", 0) == saved  # replayed call: same result, no 2nd revision
    assert tools["memory_get"]("notes.today")["entry"]["value"] == "remember this"
    assert [e["key"] for e in tools["memory_list"](prefix="notes")["entries"]] == ["notes.today"]
    conflict = tools["memory_put"]("notes.today", "x", 0 + 5)
    assert conflict["conflict"] is True and conflict["currentRevision"] == 1 and "changed in the meantime" in conflict["error"]
    assert tools["memory_delete"]("notes.today", 1)["deleted"] is True
    assert "error" in tools["memory_put"]("BAD KEY", "x", 0) and "error" in tools["memory_get"]("also bad")
    only_read = tools_for(settings, store, run, ["memory_read"])
    assert set(only_read) == {"memory_list", "memory_get"}  # no write tools without the write grant


def test_tool_calls_are_audited_and_stop_with_the_run(settings, store):
    run = store.reserve_run(prompt="p", model="m", mode="test", language="en", tools=["memory_read"], files=[])
    tools = tools_for(settings, store, run, ["memory_read"])
    tools["memory_list"]()
    assert store.tool_calls(run["id"]) == 1
    store.cancel(run["id"])
    assert "cancelled" in tools["memory_list"]()["error"]


def test_owner_api_round_trip_conflicts_and_validation(client):
    body = {"value": "first", "expected_revision": 0, "request_id": "req-1"}
    saved = client.put("/api/memory/client-acme", json=body)
    assert saved.status_code == 200 and saved.json()["revision"] == 1 and saved.json()["writer"] == "owner"
    assert client.put("/api/memory/client-acme", json=body).json() == saved.json()  # idempotent replay
    stale = client.put("/api/memory/client-acme", json={**body, "request_id": "req-2"})
    assert stale.status_code == 409 and stale.json()["detail"]["current_revision"] == 1
    assert "changed in the meantime" in stale.json()["detail"]["message"]
    assert client.get("/api/memory/client-acme").json()["entry"]["value"] == "first"
    assert client.get("/api/memory/client-acme?revision=1").json()["entry"]["revision"] == 1
    listed = client.get("/api/memory?prefix=client").json()
    assert [e["key"] for e in listed["entries"]] == ["client-acme"]
    assert client.get("/api/memory/client-acme/history").json()["versions"][0]["value"] == "first"
    second = client.put("/api/memory/client-acme", json={"value": "second", "expected_revision": 1, "request_id": "req-3"})
    assert second.json()["revision"] == 2

    assert client.put("/api/memory/BAD", json=body).status_code == 422
    assert client.put("/api/memory/client-acme", json={**body, "value": "x" * 12001}).status_code == 422
    assert client.put("/api/memory/client-acme", json={**body, "extra": 1}).status_code == 422
    assert client.put("/api/memory/client-acme", json={**body, "request_id": "has space"}).status_code == 422
    deleted = client.post("/api/memory/client-acme/delete", json={"expected_revision": 2, "request_id": "req-4"})
    assert deleted.status_code == 200 and deleted.json()["deleted"] is True
    assert client.post("/api/memory/client-acme/delete", json={"expected_revision": 3, "request_id": "req-5"}).status_code == 422
    assert client.get("/api/memory?deleted=true").json()["entries"][0]["deleted"] is True
    assert client.get("/api/memory?limit=0").status_code == 422


def test_a_run_with_memory_tools_completes_through_the_api(client):
    run_id = client.post("/api/runs", json={"prompt": "x", "mode": "test", "tools": ["memory_read", "memory_write"]}).json()["id"]
    assert wait_for_status(client, run_id)["status"] == "completed"
    assert client.post("/api/runs", json={"prompt": "x", "mode": "test", "tools": ["memory_write"]}).status_code == 422
