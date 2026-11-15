from uuid import uuid4

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from deep_agents_app.runs.metrics import MetricsCallback, extract_usage

from .conftest import wait_for_status


def result_with(usage):
    return LLMResult(generations=[[ChatGeneration(message=AIMessage(content="SECRET COMPLETION", usage_metadata=usage))]])


def test_usage_is_summed_from_usage_metadata():
    usage = {
        "input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
        "input_token_details": {"cache_read": 60}, "output_token_details": {"reasoning": 5},
    }
    assert extract_usage(result_with(usage)) == {
        "input_tokens": 100, "output_tokens": 20, "cached_tokens": 60, "reasoning_tokens": 5,
    }


def test_missing_or_odd_usage_stays_null_instead_of_zero():
    empty = {"input_tokens": None, "output_tokens": None, "cached_tokens": None, "reasoning_tokens": None}
    assert extract_usage(LLMResult(generations=[[ChatGeneration(message=AIMessage(content="x"))]])) == empty
    legacy = LLMResult(generations=[[]], llm_output={"token_usage": {"prompt_tokens": 7, "completion_tokens": True}})
    assert extract_usage(legacy) == {**empty, "input_tokens": 7}


def test_callback_records_calls_without_content(store):
    run = store.reserve_run(prompt="PRIVATE PROMPT", model="gw", mode="live", language="en", tools=[], files=[])
    callback = MetricsCallback(store, run["id"], "gw")
    ok, bad = uuid4(), uuid4()
    callback.on_chat_model_start({}, [], run_id=ok, invocation_params={"model": "claude-alias"})
    callback.on_llm_end(result_with({"input_tokens": 10, "output_tokens": 3, "total_tokens": 13}), run_id=ok)
    callback.on_chat_model_start({}, [], run_id=bad)
    callback.on_llm_error(TimeoutError("raw secret detail"), run_id=bad)
    callback.on_llm_end(result_with(None), run_id=uuid4())  # unknown call id: ignored, no error

    metrics = store.run_metrics(run["id"])
    assert metrics["totals"]["calls"] == 2 and metrics["totals"]["errors"] == 1
    assert metrics["totals"]["input_tokens"] == 10 and metrics["totals"]["cached_tokens"] is None
    first, second = metrics["calls"]
    assert (first["model"], first["status"], first["error"]) == ("claude-alias", "ok", "")
    assert (second["model"], second["status"], second["error"]) == ("gw", "error", "error_gateway_unavailable")
    stored = str(metrics)
    assert "SECRET COMPLETION" not in stored and "PRIVATE PROMPT" not in stored and "raw secret detail" not in stored


def test_a_failing_metrics_store_never_breaks_the_caller(store, monkeypatch):
    callback = MetricsCallback(store, "not-a-run", "gw")
    monkeypatch.setattr(store, "record_llm_call", lambda **kw: 1 / 0)
    call = uuid4()
    callback.on_chat_model_start({}, [], run_id=call)
    callback.on_llm_end(result_with(None), run_id=call)  # must not raise


def test_a_run_records_its_model_calls_and_the_api_reports_them(client):
    run_id = client.post("/api/runs", json={"prompt": "hello", "mode": "test"}).json()["id"]
    wait_for_status(client, run_id)
    metrics = client.get(f"/api/runs/{run_id}/metrics").json()
    assert metrics["totals"]["calls"] >= 1 and metrics["totals"]["errors"] == 0
    assert all(call["status"] == "ok" and call["duration_ms"] >= 0 for call in metrics["calls"])

    summary = client.get("/api/metrics/summary?days=1").json()
    assert summary["days"] == 1
    row = next(m for m in summary["models"] if m["model"] == metrics["calls"][0]["model"])
    assert row["calls"] >= 1 and row["errors"] == 0 and row["avg_duration_ms"] is not None


def test_metrics_api_validation(client):
    assert client.get("/api/metrics/summary?days=0").status_code == 422
    assert client.get("/api/metrics/summary?days=91").status_code == 422
    assert client.get("/api/runs/not-a-uuid/metrics").status_code == 404
    assert client.get("/api/runs/00000000-0000-0000-0000-000000000000/metrics").status_code == 404
    assert client.get("/api/metrics/summary").json() == {"days": 7, "models": []}


def test_failed_model_call_is_visible_in_metrics_in_the_run_language(database_url, tmp_path):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, text

    from deep_agents_app.main import create_app

    from .conftest import make_settings

    # Unreachable gateway: the run fails, and the failed call is recorded without any raw error text.
    settings = make_settings(database_url, "t_metrics_x5", tmp_path, llm_gateway_url="http://127.0.0.1:9/v1", llm_max_retries=0, llm_timeout_seconds=3, app_language="hu")
    try:
        with TestClient(create_app(settings)) as client:
            run_id = client.post("/api/runs", json={"prompt": "x"}).json()["id"]
            run = wait_for_status(client, run_id)
            metrics = client.get(f"/api/runs/{run_id}/metrics").json()
        assert run["status"] == "failed" and "modellgateway" in run["error"]
        assert metrics["totals"]["errors"] == 1 and "modellgateway" in metrics["calls"][0]["error"]
    finally:
        engine = create_engine(settings.sqlalchemy_url)
        with engine.begin() as db:
            db.execute(text('DROP SCHEMA IF EXISTS "t_metrics_x5" CASCADE'))
        engine.dispose()
