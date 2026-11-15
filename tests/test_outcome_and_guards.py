import json

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage

from deep_agents_app.agents import runner
from deep_agents_app.runs.outcome import assess_outcome, validation_failures

from .conftest import wait_for_status


def tool_message(content, status="success", name="webfetch"):
    return ToolMessage(content=content if isinstance(content, str) else json.dumps(content), tool_call_id="c", name=name, status=status)


def test_validation_failures_count_error_results_only():
    messages = [
        tool_message({"text": "fine"}), tool_message({"error": "denied"}), tool_message({"partial": True}),
        tool_message("not json"), tool_message("x", status="error"), AIMessage(content="{}"),
    ]
    assert validation_failures(messages) == 3


def test_outcome_is_blocked_by_tool_problems_even_when_the_text_claims_success():
    claim = json.dumps({"outcome": {"status": "success", "reason": "done"}})
    assert assess_outcome(claim, [tool_message({"error": "x"})]) == "blocked"
    assert assess_outcome(claim, [tool_message({"conflict": True})]) == "blocked"
    assert assess_outcome(claim, [], audit_errors=1) == "blocked"
    assert assess_outcome(claim, [tool_message({"text": "ok"})]) == "success"


def test_outcome_envelope_must_be_exact_otherwise_unknown():
    assert assess_outcome("plain markdown", []) == "unknown"
    assert assess_outcome(json.dumps({"outcome": {"status": "noop", "reason": "nothing to do"}}), []) == "noop"
    for bad in (
        {"outcome": {"status": "great", "reason": "x"}}, {"outcome": {"status": "noop"}}, {"outcome": "noop"},
        {"outcome": {"status": "noop", "reason": "x" * 1001}}, {"outcome": {"status": "noop", "reason": "x", "extra": 1}},
    ):
        assert assess_outcome(json.dumps(bad), []) == "unknown"


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def call_fetch(index):
    return AIMessage(content="", tool_calls=[{"name": "webfetch", "args": {"url": "http://not-allowed.example/"}, "id": f"call{index}"}])


def start(client, monkeypatch, responses):
    monkeypatch.setattr(runner, "build_test_model", lambda prompt: ScriptedModel(responses=responses))
    run_id = client.post("/api/runs", json={"prompt": "go", "mode": "test", "tools": ["webfetch"]}).json()["id"]
    return wait_for_status(client, run_id, seconds=60)


def test_repeated_tool_failures_stop_the_run(client, monkeypatch):
    run = start(client, monkeypatch, [call_fetch(i) for i in range(8)] + [AIMessage(content="never reached")])
    assert run["status"] == "failed" and run["outcome_status"] == "blocked"
    assert "Repeated tool errors" in run["error"]
    metrics = client.get(f"/api/runs/{run['id']}/metrics").json()
    assert 3 <= metrics["summary"]["tool_calls"] <= 5 and {t["outcome"] for t in metrics["tools"]} == {"error"}


def test_a_completed_run_whose_tool_failed_is_reported_as_blocked(client, monkeypatch):
    run = start(client, monkeypatch, [call_fetch(0), AIMessage(content="I could not fetch it, sorry.")])
    assert run["status"] == "completed" and run["outcome_status"] == "blocked"


def test_a_run_can_state_its_own_business_outcome(client, monkeypatch):
    envelope = json.dumps({"outcome": {"status": "noop", "reason": "nothing new"}})
    run = start(client, monkeypatch, [AIMessage(content=envelope)])
    assert run["status"] == "completed" and run["outcome_status"] == "noop"
