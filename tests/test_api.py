from fastapi.testclient import TestClient

from deep_agents_app.main import create_app
from deep_agents_app.tools.registry import ToolContext, build_tools

from .conftest import make_settings, wait_for_status


def start(client, **body):
    return client.post("/api/runs", json={"prompt": "hello", "mode": "test", **body})


def test_healthz_and_index_need_no_login(client):
    assert client.get("/healthz").json() == {"status": "ok", "database": True, "auth": False}
    page = client.get("/")
    assert page.status_code == 200 and "Deep Agents" in page.text


def test_tool_catalog_lists_builtin_tools(client):
    names = {tool["name"] for tool in client.get("/api/tools").json()}
    assert names == {"webfetch", "websearch", "github", "files", "document", "report"}


def test_offline_run_completes_with_events_and_state(client):
    created = start(client)
    assert created.status_code == 201
    run_id = created.json()["id"]
    run = wait_for_status(client, run_id)
    assert run["status"] == "completed" and "Test report" in run["result"] and run["error"] == ""

    state = client.get(f"/api/langgraph/threads/{run_id}/state").json()
    assert state["metadata"]["status"] == "completed" and state["metadata"]["stream_cursor"] > 0
    assert state["values"]["messages"][0]["content"] == "hello"

    with client.stream("GET", f"/api/langgraph/threads/{run_id}/runs/{run_id}/stream") as response:
        text = "".join(response.iter_text())
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: metadata" in text and "event: end" in text and "Test report" in text

    cursor = state["metadata"]["stream_cursor"]
    with client.stream("GET", f"/api/langgraph/threads/{run_id}/runs/{run_id}/stream", headers={"last-event-id": str(cursor)}) as r:
        assert "event: metadata" not in "".join(r.iter_text())  # resumes after the cursor
    assert client.get(f"/api/langgraph/threads/{run_id}/runs/{run_id}/stream", headers={"last-event-id": "999999999"}).status_code == 400
    assert any(r["id"] == run_id for r in client.get("/api/runs").json())


def test_request_validation(client):
    assert start(client, tools=["nope"]).status_code == 422
    assert start(client, tools=["document"]).status_code == 422  # requires "files"
    assert start(client, tools=["files"], files=["0" * 32]).status_code == 422
    assert client.post("/api/runs", json={"prompt": "", "mode": "test"}).status_code == 422
    assert client.get("/api/runs/not-a-uuid").status_code == 404
    assert client.get("/api/langgraph/threads/00000000-0000-0000-0000-000000000000/state").status_code == 404


def test_test_mode_can_be_disabled(database_url, tmp_path):
    settings = make_settings(database_url, "t_notest_x1", tmp_path, allow_test_mode=False)
    try:
        with TestClient(create_app(settings)) as client:
            assert start(client).status_code == 403
    finally:
        _drop(settings)


def test_live_run_without_gateway_key_fails_explicitly_in_the_run_language(database_url, tmp_path):
    settings = make_settings(database_url, "t_nokey_x2", tmp_path, llm_api_key="", app_language="hu")
    try:
        with TestClient(create_app(settings)) as client:
            run_id = client.post("/api/runs", json={"prompt": "x"}).json()["id"]
            run = wait_for_status(client, run_id)
        assert run["status"] == "failed" and "gateway" in run["error"].lower() and "nincs beállítva" in run["error"]
    finally:
        _drop(settings)


def test_cancel_is_idempotent(client):
    store = client.app.state.store
    run = store.reserve_run(prompt="p", model="m", mode="test", language="en", tools=[], files=[])
    assert client.post(f"/api/runs/{run['id']}/cancel").json() == {"cancelled": True}
    assert client.post(f"/api/runs/{run['id']}/cancel").json() == {"cancelled": False}
    assert client.get(f"/api/runs/{run['id']}").json()["status"] == "cancelled"


def test_upload_limits_and_file_tools(client, settings):
    assert client.post("/api/files?name=a.txt", content=b"").status_code == 400
    assert client.post("/api/files?name=a.txt", content=b"x" * (2 * 1024 * 1024 + 1)).status_code == 413
    uploaded = client.post("/api/files?name=../../evil/notes.txt", content="héllo wörld".encode()).json()
    assert uploaded["name"] == "notes.txt"

    run = start(client, tools=["files", "document"], files=[uploaded["id"]]).json()
    wait_for_status(client, run["id"])
    store = client.app.state.store
    live = store.reserve_run(prompt="p", model="m", mode="test", language="en", tools=["files", "document"], files=[uploaded["id"]])
    tools = {t.__name__: t for t in build_tools(["files", "document"], ToolContext(settings, store, live["id"], "en", [uploaded["id"]]))}
    assert tools["file_list"]() == [{"id": uploaded["id"], "name": "notes.txt", "size": uploaded["size"]}]
    assert tools["file_read"](uploaded["id"])["content"] == "héllo wörld"
    assert tools["document_read"](uploaded["id"])["text"] == "héllo wörld"
    assert "error" in tools["file_read"]("f" * 32)  # not attached to this run
    assert "error" in tools["file_read"](uploaded["id"], offset=-1)
    # Tampering with the stored blob is detected.
    (settings.data_dir / "inputs" / f"{uploaded['id']}.blob").write_bytes(b"changed!!!!!")
    assert "error" in tools["file_read"](uploaded["id"])
    assert [(a["tool"], a["outcome"]) for a in _audit(store, live["id"])][-2:] == [("files", "error"), ("files", "error")]


def test_reports_are_private_idempotent_and_downloadable(client, settings):
    store = client.app.state.store
    live = store.reserve_run(prompt="p", model="m", mode="test", language="en", tools=["report"], files=[])
    (report,) = build_tools(["report"], ToolContext(settings, store, live["id"], "en"))
    made = report("Weekly", "md", text="# Hi")
    assert report("Weekly", "md", text="# Hi")["reused"] is True
    csv_made = report("Table", "csv", rows=[["=cmd|' /C calc'!A0", "ok"]])
    xlsx_made = report("Sheet", "xlsx", rows=[["=1+1", "b"]])
    assert report("Doc", "pdf", text="line\n" * 50)["size"] > 0
    assert "error" in report("x", "exe", text="y") and "error" in report("x", "csv", rows=[])

    download = client.get(f"/api/runs/{live['id']}/artifacts/{made['id']}")
    assert download.status_code == 200 and download.content == b"# Hi"
    assert download.headers["content-security-policy"] == "sandbox" and "no-store" in download.headers["cache-control"]
    assert client.get(f"/api/runs/{live['id']}/artifacts/{csv_made['id']}").content.startswith(b"'=cmd")
    from io import BytesIO

    from openpyxl import load_workbook

    cell = load_workbook(BytesIO(client.get(f"/api/runs/{live['id']}/artifacts/{xlsx_made['id']}").content)).active["A1"]
    assert cell.data_type == "s" and cell.value == "'=1+1"
    assert client.get(f"/api/runs/{'0' * 8}-0000-0000-0000-{'0' * 12}/artifacts/{made['id']}").status_code == 404
    names = {a["name"] for a in client.get(f"/api/runs/{live['id']}").json()["artifacts"]}
    assert len(names) == 4  # md (replayed, not duplicated), csv, xlsx, pdf


def test_tool_calls_are_audited_and_capped(client, settings, monkeypatch):
    store = client.app.state.store
    live = store.reserve_run(prompt="p", model="m", mode="test", language="en", tools=["report"], files=[])
    (report,) = build_tools(["report"], ToolContext(settings, store, live["id"], "en"))
    report("t", "md", text="a")
    assert [(a["tool"], a["outcome"]) for a in _audit(store, live["id"])] == [("report", "ok")]
    monkeypatch.setattr("deep_agents_app.tools.registry.MAX_TOOL_CALLS_PER_RUN", 1)
    assert "limit" in report("t2", "md", text="b")["error"]
    store.cancel(live["id"])
    assert "cancelled" in report("t3", "md", text="c")["error"]


def _audit(store, run_id):
    from sqlalchemy import select

    from deep_agents_app.db.schema import tool_audit

    with store.engine.connect() as db:
        return [dict(r) for r in db.execute(select(tool_audit).where(tool_audit.c.run_id == run_id).order_by(tool_audit.c.id)).mappings()]


def _drop(settings):
    from sqlalchemy import create_engine, text

    engine = create_engine(settings.sqlalchemy_url)
    with engine.begin() as db:
        db.execute(text(f'DROP SCHEMA IF EXISTS "{settings.database_schema}" CASCADE'))
    engine.dispose()

