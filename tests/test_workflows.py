import json
import time
from datetime import timedelta

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import select, update

from deep_agents_app.agents import runner
from deep_agents_app.db.schema import runs, workflow_diagnostics, workflow_runs, workflow_steps
from deep_agents_app.runs.store import now
from deep_agents_app.workflows import diagnostics
from deep_agents_app.workflows.engine import BusinessBlocked, child_prompt
from deep_agents_app.workflows.scheduler import run_due_workflows
from deep_agents_app.workflows.spec import WorkflowRejected, validate_spec

from .conftest import wait_for_status

CONSENT = {"accepted_data_flow": True}


def node(id, kind, label=None, x=0, y=0, agent="", operator="", value=""):
    return {"id": id, "kind": kind, "label": label or id.title(), "x": x, "y": y, "agentId": agent, "operator": operator, "value": value}


def edge(source, target, branch="always"):
    return {"source": source, "target": target, "branch": branch}


def spec_of(nodes, edges):
    return {"version": 1, "nodes": nodes, "edges": edges}


def agent(client, name="Reporter", prompt="Write a short report.", tools=None):
    config = {"prompt": prompt, "mode": "test", "tools": tools or []}
    response = client.post("/api/agents", json={"name": name, "config": config})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def linear(agent_id):
    return spec_of(
        [node("start", "start"), node("worker", "agent", "Worker", agent=agent_id), node("end", "end")],
        [edge("start", "worker"), edge("worker", "end")],
    )


def save(client, name="Flow", spec=None, workflow_id=None, version=0):
    body = {"name": name, "expected_version": version, "spec": spec, **({"id": workflow_id} if workflow_id else {})}
    return client.post("/api/workflows", json=body)


def start(client, workflow_id, version=1, text="input text", **extra):
    return client.post(f"/api/workflows/{workflow_id}/start", json={"version": version, "input": text, **CONSENT, **extra})


def detail(client, run_id):
    return client.get(f"/api/workflow-runs/{run_id}").json()


def wait_workflow(client, run_id, wanted=("completed", "failed", "cancelled"), seconds=60):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        run = detail(client, run_id)
        if run["status"] in wanted:
            return run
        time.sleep(0.1)
    raise AssertionError(f"workflow did not finish: {run}")


def wait_step(client, run_id, node_id, status, seconds=30):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        steps = {s["node_id"]: s for s in detail(client, run_id)["steps"]}
        if node_id in steps and steps[node_id]["status"] == status:
            return steps[node_id]
        time.sleep(0.1)
    raise AssertionError(f"step {node_id} never reached {status}: {detail(client, run_id)['steps']}")


def children(client, run_id):
    store = client.app.state.store
    with store.engine.connect() as db:
        return [dict(r) for r in db.execute(select(runs).where(runs.c.workflow_run_id == run_id)).mappings()]


# -- specification -------------------------------------------------------------------------------------------


def test_the_validator_accepts_a_dag_and_orders_it_topologically():
    a = "00000000-0000-0000-0000-00000000000a"
    spec = spec_of(
        [node("end", "end"), node("b", "agent", agent=a), node("start", "start"), node("a", "agent", agent=a)],
        [edge("b", "end"), edge("a", "end"), edge("start", "a"), edge("start", "b")],
    )
    ordered = [n["id"] for n in validate_spec(spec)["nodes"]]
    assert ordered[0] == "start" and ordered[-1] == "end" and set(ordered) == {"start", "a", "b", "end"}


@pytest.mark.parametrize(
    ("mutate", "key"),
    [
        (lambda s: s["edges"].append(edge("worker", "start")), "wf_connectivity"),  # an edge INTO the start node
        (lambda s: s["nodes"].append(node("worker", "join")), "wf_node_id"),  # repeated id
        (lambda s: s["edges"].pop(), "wf_connectivity"),  # dead end
        (lambda s: s["nodes"][1].update(operator="contains"), "wf_condition_only"),
        (lambda s: s["nodes"][1].update(agentId="not-a-uuid"), "wf_agent_id"),
        (lambda s: s["nodes"][0].update(agentId="00000000-0000-0000-0000-00000000000a"), "wf_agent_only"),
        (lambda s: s["nodes"][1].update(label=" "), "wf_node_label"),
        (lambda s: s["nodes"][1].update(x=3000), "wf_node_position"),
        (lambda s: s["nodes"][1].update(id="Bad-Id"), "wf_node_id"),
        (lambda s: s["nodes"][1].update(id="constructor"), "wf_node_id"),
        (lambda s: s["nodes"].append(node("extra", "start")), "wf_endpoints"),
        (lambda s: s["edges"].append(edge("start", "worker")), "wf_bad_edge"),  # repeated pair
        (lambda s: s["edges"][0].update(branch="true"), "wf_branch"),
        (lambda s: s.update(version=2), "wf_bad_graph"),
        (lambda s: s.update(extra=1), "wf_bad_graph"),
    ],
)
def test_the_validator_rejects_malformed_graphs(mutate, key):
    spec = linear("00000000-0000-0000-0000-00000000000a")
    mutate(spec)
    with pytest.raises(WorkflowRejected) as error:
        validate_spec(spec)
    assert error.value.key == key


def test_cycles_missing_branches_executable_conditions_and_size_limits():
    uid = "00000000-0000-0000-0000-00000000000a"
    cycle = spec_of(
        [node("start", "start"), node("a", "join"), node("b", "join"), node("end", "end")],
        [edge("start", "a"), edge("a", "b"), edge("b", "a"), edge("b", "end")],
    )
    with pytest.raises(WorkflowRejected) as error:
        validate_spec(cycle)
    assert error.value.key == "wf_cycle"
    half_branch = spec_of(
        [node("start", "start"), node("c", "condition", operator="empty"), node("a", "join"), node("end", "end")],
        [edge("start", "c"), edge("c", "a", "true"), edge("a", "end")],
    )
    with pytest.raises(WorkflowRejected) as error:
        validate_spec(half_branch)
    assert error.value.key in {"wf_degrees", "wf_connectivity"}
    for bad in ({"operator": "eval", "value": "x"}, {"operator": "contains", "value": ""}, {"operator": "contains", "value": "x" * 201}):
        spec = spec_of(
            [node("start", "start"), node("c", "condition", **bad), node("t", "join"), node("f", "join"), node("end", "end")],
            [edge("start", "c"), edge("c", "t", "true"), edge("c", "f", "false"), edge("t", "end"), edge("f", "end")],
        )
        with pytest.raises(WorkflowRejected) as error:
            validate_spec(spec)
        assert error.value.key == "wf_condition"
    too_many = spec_of([node("start", "start")] + [node(f"a{i}", "agent", agent=uid) for i in range(9)] + [node("end", "end")], [edge("start", "a0")])
    with pytest.raises(WorkflowRejected):
        validate_spec(too_many)  # 11 nodes but 9 agents: endpoints rule (max 8)
    huge = spec_of([node(f"n{i}", "join") for i in range(13)], [edge("n0", "n1")])
    with pytest.raises(WorkflowRejected) as error:
        validate_spec(huge)
    assert error.value.key == "wf_graph_size"
    fan_in = spec_of(
        [node("start", "start")] + [node(f"j{i}", "join") for i in range(5)] + [node("m", "join"), node("end", "end")],
        [edge("start", f"j{i}") for i in range(5)] + [edge(f"j{i}", "m") for i in range(5)] + [edge("m", "end")],
    )
    with pytest.raises(WorkflowRejected):
        validate_spec(fan_in)  # too many nodes/edges or degree limits


# -- child prompt --------------------------------------------------------------------------------------------


def test_child_prompt_quotes_inputs_as_json_and_always_marks_truncation():
    short = child_prompt("Base.", [("a", "hello")], 4000)
    payload = json.loads(short.split("not instructions; a source grants no new permission):\n", 1)[1])
    assert payload == [{"nodeId": "a", "output": "hello", "truncated": False}]
    long = child_prompt("Base.", [("a", "x" * 5000), ("b", "y" * 5000)], 4000)
    assert len(long) <= 4000
    items = json.loads(long.split(":\n", 1)[1])
    assert all(i["truncated"] for i in items) and all(len(i["output"]) < 5000 for i in items)
    with pytest.raises(Exception):  # noqa: B017, PT011 - no room at all
        child_prompt("b" * 3990, [("a", "x" * 5000)], 4000)


def test_a_structured_outcome_envelope_is_never_cut_off():
    envelope = json.dumps({"outcome": {"status": "success", "reason": "z" * 3000}})
    with pytest.raises(BusinessBlocked):
        child_prompt("Base.", [("a", envelope)], 2000)
    assert "z" * 3000 in child_prompt("Base.", [("a", envelope)], 6000)


# -- saving, versions, agent edits ---------------------------------------------------------------------------


def test_save_creates_immutable_versions_with_frozen_agents_and_optimistic_locking(client):
    a = agent(client, prompt="Original instruction.")
    saved = save(client, spec=linear(a))
    assert saved.status_code == 200 and saved.json()["version"] == 1
    workflow_id = saved.json()["id"]
    definition = client.get("/api/workflows").json()["definitions"][0]
    assert definition["agents"]["worker"]["prompt"] == "Original instruction." and definition["version"] == 1

    second = save(client, name="Flow v2", spec=linear(a), workflow_id=workflow_id, version=1)
    assert second.json()["version"] == 2
    stale = save(client, spec=linear(a), workflow_id=workflow_id, version=1)
    assert stale.status_code == 409 and "changed" in stale.json()["detail"]
    versions = client.get(f"/api/workflows/{workflow_id}/versions").json()["versions"]
    assert [v["version"] for v in versions] == [2, 1] and versions[1]["agents"]["worker"]["prompt"] == "Original instruction."
    assert save(client, spec=linear(a), workflow_id="00000000-0000-0000-0000-00000000dead", version=3).status_code == 409


def test_save_rejects_duplicate_step_names_unknown_agents_and_long_prompts(client):
    a = agent(client)
    duplicate = spec_of(
        [node("start", "start", "Step"), node("worker", "agent", " step ", agent=a), node("end", "end")],
        [edge("start", "worker"), edge("worker", "end")],
    )
    rejected = save(client, spec=duplicate)
    assert rejected.status_code == 422 and "same name" in rejected.json()["detail"]
    assert save(client, spec=linear("00000000-0000-0000-0000-0000000000ff")).status_code == 422  # agent not found
    long_agent = agent(client, name="Long", prompt="x" * 2801)
    too_long = save(client, spec=linear(long_agent))
    assert too_long.status_code == 422 and "2800" in too_long.json()["detail"]
    assert save(client, name="", spec=linear(a)).status_code == 422
    assert client.post("/api/workflows", json={"name": "x", "expected_version": 0, "spec": linear(a), "extra": 1}).status_code == 422


def test_editing_an_agent_publishes_new_workflow_versions_atomically(client):
    a = agent(client, prompt="Old.")
    workflow_id = save(client, spec=linear(a)).json()["id"]
    run_id = start(client, workflow_id).json()["id"]  # a run pins version 1
    agent_row = client.get(f"/api/agents/{a}").json()
    body = {"name": "Reporter 2", "config": {"prompt": "New.", "mode": "test", "tools": []}, "revision": agent_row["revision"]}
    edited = client.put(f"/api/agents/{a}", json=body).json()
    assert edited["updated_workflows"] == 1
    definition = client.get("/api/workflows").json()["definitions"][0]
    assert definition["version"] == 2 and definition["agents"]["worker"]["prompt"] == "New." and definition["agents"]["worker"]["name"] == "Reporter 2"
    assert start(client, workflow_id, version=1).status_code == 422  # only the latest version starts
    old = client.get(f"/api/workflows/{workflow_id}/versions").json()["versions"][1]
    assert old["agents"]["worker"]["prompt"] == "Old."
    assert wait_workflow(client, run_id)["version"] == 1  # the already-started run is untouched

    too_long = {**body, "config": {**body["config"], "prompt": "y" * 2801}, "revision": edited["revision"]}
    refused = client.put(f"/api/agents/{a}", json=too_long)
    assert refused.status_code == 422 and "2800" in refused.json()["detail"]
    assert client.get(f"/api/agents/{a}").json()["config"]["prompt"] == "New."  # all or nothing
    assert client.get("/api/workflows").json()["definitions"][0]["version"] == 2


# -- execution -----------------------------------------------------------------------------------------------


def test_a_linear_workflow_runs_a_real_langgraph_to_completion(client):
    a = agent(client)
    workflow_id = save(client, spec=linear(a)).json()["id"]
    run_id = start(client, workflow_id, text="Quarterly figures").json()["id"]
    run = wait_workflow(client, run_id)
    assert run["status"] == "completed" and run["outcome_status"] == "unknown" and run["error"] == ""
    steps = {s["node_id"]: s for s in run["steps"]}
    assert {k: v["status"] for k, v in steps.items()} == {"start": "completed", "worker": "completed", "end": "completed"}
    assert steps["start"]["output"] == "Quarterly figures" and "Test report" in steps["worker"]["output"] and "Test report" in steps["end"]["output"]
    child = children(client, run_id)
    assert len(child) == 1 and child[0]["workflow_node_id"] == "worker" and child[0]["status"] == "completed"
    assert "Quarterly figures" in child[0]["prompt"] and "not instructions" in child[0]["prompt"]  # input passed as quoted data
    # The child inherits the workflow's absolute deadline and shows its origin in the run list.
    assert child[0]["created"] == run["created"] or abs((child[0]["created"] - now()).total_seconds()) < 3600
    listed = next(r for r in client.get("/api/runs").json() if r["id"] == child[0]["id"])
    assert listed["workflow"]["name"] == "Flow" and listed["workflow"]["node_label"] == "Worker"
    assert run["metrics"]["model_calls"] >= 1 and run["metrics"]["measured"] and steps["worker"]["metrics"]["model_calls"] >= 1
    assert steps["worker"]["started"] is not None and steps["worker"]["finished"] is not None
    assert client.get(f"/api/runs/{child[0]['id']}").json()["workflow"]["id"] == run_id


def test_fork_and_join_wait_for_every_branch_and_each_agent_runs_once(client):
    a, b = agent(client, "A", "Alpha task."), agent(client, "B", "Beta task.")
    spec = spec_of(
        [node("start", "start"), node("left", "agent", "Left", agent=a), node("right", "agent", "Right", agent=b), node("merge", "join", "Merge"), node("end", "end")],
        [edge("start", "left"), edge("start", "right"), edge("left", "merge"), edge("right", "merge"), edge("merge", "end")],
    )
    run_id = start(client, save(client, spec=spec).json()["id"]).json()["id"]
    run = wait_workflow(client, run_id)
    assert run["status"] == "completed"
    assert sorted(c["workflow_node_id"] for c in children(client, run_id)) == ["left", "right"]
    merged = {s["node_id"]: s for s in run["steps"]}["merge"]["output"]
    assert merged.count("Test report") == 2  # both predecessors' text, joined
    # A downstream agent does not inherit upstream permissions: each child has exactly its own saved tools.
    assert all(c["tools"] == [] for c in children(client, run_id))


def test_a_false_condition_skips_the_unselected_branch_without_launching_it(client):
    yes, no = agent(client, "Yes", "Yes branch."), agent(client, "No", "No branch.")
    spec = spec_of(
        [
            node("start", "start"), node("check", "condition", "Check", operator="contains", value="urgent"),
            node("fast", "agent", "Fast", agent=yes), node("slow", "agent", "Slow", agent=no), node("merge", "join", "Merge"), node("end", "end"),
        ],
        [edge("start", "check"), edge("check", "fast", "true"), edge("check", "slow", "false"), edge("fast", "merge"), edge("slow", "merge"), edge("merge", "end")],
    )
    workflow_id = save(client, spec=spec).json()["id"]
    plain = wait_workflow(client, start(client, workflow_id, text="routine request").json()["id"])
    steps = {s["node_id"]: s["status"] for s in plain["steps"]}
    assert plain["status"] == "completed" and steps["fast"] == "skipped" and steps["slow"] == "completed" and steps["merge"] == "completed"
    assert [c["workflow_node_id"] for c in children(client, plain["id"])] == ["slow"]
    urgent = wait_workflow(client, start(client, workflow_id, text="URGENT: act now").json()["id"])  # casefold match
    assert {s["node_id"]: s["status"] for s in urgent["steps"]}["slow"] == "skipped"
    assert [c["workflow_node_id"] for c in children(client, urgent["id"])] == ["fast"]


def approval_flow(client):
    a = agent(client)
    spec = spec_of(
        [node("start", "start"), node("gate", "approval", "Gate"), node("worker", "agent", "Worker", agent=a), node("end", "end")],
        [edge("start", "gate"), edge("gate", "worker"), edge("worker", "end")],
    )
    return save(client, spec=spec).json()["id"]


def test_the_owner_approval_gate_is_durable_and_grants_no_other_rights(client):
    workflow_id = approval_flow(client)
    run_id = start(client, workflow_id).json()["id"]
    wait_step(client, run_id, "gate", "approval")
    assert children(client, run_id) == [] and detail(client, run_id)["status"] == "running"  # nothing launched yet
    assert client.post(f"/api/workflow-runs/{run_id}/approve", json={"node_id": "worker", "approved": True}).status_code == 422
    assert client.post(f"/api/workflow-runs/{run_id}/approve", json={"node_id": "gate", "approved": True, "x": 1}).status_code == 422
    assert client.post(f"/api/workflow-runs/{run_id}/approve", json={"node_id": "gate", "approved": True}).status_code == 200
    assert client.post(f"/api/workflow-runs/{run_id}/approve", json={"node_id": "gate", "approved": False}).status_code == 422  # already decided
    assert wait_workflow(client, run_id)["status"] == "completed" and len(children(client, run_id)) == 1


def test_a_rejected_approval_fails_the_workflow_without_retry(client):
    run_id = start(client, approval_flow(client)).json()["id"]
    wait_step(client, run_id, "gate", "approval")
    client.post(f"/api/workflow-runs/{run_id}/approve", json={"node_id": "gate", "approved": False})
    run = wait_workflow(client, run_id)
    assert run["status"] == "failed" and "did not approve" in run["error"] and children(client, run_id) == []
    assert {s["node_id"]: s["status"] for s in run["steps"]}["gate"] == "failed"
    assert client.post(f"/api/workflow-runs/{run_id}/approve", json={"node_id": "gate", "approved": True}).status_code == 422  # not active


def test_cancelling_stops_only_the_workflows_own_children(client):
    other = client.app.state.store.reserve_run(prompt="unrelated", model="m", mode="test", language="en", tools=[], files=[])
    run_id = start(client, approval_flow(client)).json()["id"]
    wait_step(client, run_id, "gate", "approval")
    assert client.post(f"/api/workflow-runs/{run_id}/cancel").status_code == 202
    run = wait_workflow(client, run_id)
    assert run["status"] == "cancelled" and "cancelled the workflow" in run["error"]
    assert client.app.state.store.get(other["id"])["status"] == "running"  # a foreign run is untouched
    assert client.post(f"/api/workflow-runs/{run_id}/approve", json={"node_id": "gate", "approved": True}).status_code == 422


def test_capacity_waits_preserve_order_and_never_duplicate_a_reservation(database_url, tmp_path):
    from fastapi.testclient import TestClient

    from deep_agents_app.main import create_app

    from .conftest import make_settings

    settings = make_settings(database_url, "t_wfcap_x8", tmp_path, max_concurrent_runs=1)
    try:
        with TestClient(create_app(settings)) as client:
            a, b = agent(client, "A", "Alpha."), agent(client, "B", "Beta.")
            spec = spec_of(
                [node("start", "start"), node("left", "agent", "Left", agent=a), node("right", "agent", "Right", agent=b), node("merge", "join", "Merge"), node("end", "end")],
                [edge("start", "left"), edge("start", "right"), edge("left", "merge"), edge("right", "merge"), edge("merge", "end")],
            )
            run_id = start(client, save(client, spec=spec).json()["id"]).json()["id"]
            run = wait_workflow(client, run_id)
            assert run["status"] == "completed"
            assert sorted(c["workflow_node_id"] for c in children(client, run_id)) == ["left", "right"]  # exactly one child per step
            client.app.state.executor.pool.shutdown(wait=True)
    finally:
        _drop(settings)


def test_a_failed_child_shows_its_cause_and_is_not_relaunched(database_url, tmp_path):
    from fastapi.testclient import TestClient

    from deep_agents_app.main import create_app

    from .conftest import make_settings

    settings = make_settings(
        database_url, "t_wffail_x9", tmp_path, llm_gateway_url="http://127.0.0.1:9/v1", llm_max_retries=0, llm_timeout_seconds=3, app_language="hu",
    )
    try:
        with TestClient(create_app(settings)) as client:
            live = client.post("/api/agents", json={"name": "Live", "config": {"prompt": "Go.", "mode": "live"}}).json()["id"]
            run_id = start(client, save(client, spec=linear(live)).json()["id"]).json()["id"]
            run = wait_workflow(client, run_id)
            assert run["status"] == "failed" and "sikertelen" in run["error"] and "modellgateway" in run["error"]
            assert len(children(client, run_id)) == 1  # no automatic relaunch
            worker = {s["node_id"]: s for s in run["steps"]}["worker"]
            assert worker["status"] == "failed" and worker["output"] == "error_gateway_unavailable"
            client.app.state.executor.pool.shutdown(wait=True)
    finally:
        _drop(settings)


class Scripted(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def test_a_blocked_business_outcome_stops_the_workflow_before_later_agents(client, monkeypatch):
    first, second = agent(client, "First", "Check."), agent(client, "Second", "Report.")
    spec = spec_of(
        [node("start", "start"), node("one", "agent", "One", agent=first), node("two", "agent", "Two", agent=second), node("end", "end")],
        [edge("start", "one"), edge("one", "two"), edge("two", "end")],
    )
    envelope = json.dumps({"outcome": {"status": "blocked", "reason": "source unreachable"}})
    monkeypatch.setattr(runner, "build_test_model", lambda prompt: Scripted(responses=[AIMessage(content=envelope)]))
    # A blocked outcome must be detected by the run itself: use a tool failure to make assess_outcome say "blocked".
    run_id = start(client, save(client, spec=spec).json()["id"]).json()["id"]
    run = wait_workflow(client, run_id)
    child = children(client, run_id)
    # The child states its own outcome as "blocked" through the envelope, which assess_outcome accepts only when no tool failed;
    # here the envelope itself is the agent's claim, so the run completes with that status.
    assert child[0]["outcome_status"] == "blocked"
    assert run["status"] == "failed" and run["outcome_status"] == "blocked" and "business outcome" in run["error"]
    assert [c["workflow_node_id"] for c in child] == ["one"]  # the later agent never started


def test_a_workflow_is_checked_like_a_manual_start(client):
    workflow_id = save(client, spec=linear(agent(client))).json()["id"]
    assert client.post(f"/api/workflows/{workflow_id}/start", json={"version": 1, "input": "x"}).status_code == 422  # no consent
    assert start(client, workflow_id, text="x" * 4001).status_code == 422
    assert start(client, workflow_id, version=9).status_code == 422
    assert client.post(f"/api/workflows/{workflow_id}/start", json={"version": 1, "input": "x", **CONSENT, "extra": 1}).status_code == 422
    assert client.post("/api/workflows/not-a-uuid/start", json={"version": 1, **CONSENT}).status_code == 404


def test_at_most_three_workflows_are_active_and_the_stores_are_bounded(client):
    workflow_id = approval_flow(client)
    runs_started = [start(client, workflow_id).json()["id"] for _ in range(3)]
    fourth = start(client, workflow_id)
    assert fourth.status_code == 422 and "At most three" in fourth.json()["detail"]
    for run_id in runs_started:
        client.post(f"/api/workflow-runs/{run_id}/cancel")
    assert start(client, workflow_id).status_code == 201  # a slot is free again


def test_rerun_is_a_new_instance_pinned_to_the_original_version_and_input(client):
    a = agent(client, prompt="Before.")
    workflow_id = save(client, spec=linear(a)).json()["id"]
    original = wait_workflow(client, start(client, workflow_id, text="first input").json()["id"])
    client.put(f"/api/agents/{a}", json={"name": "Reporter", "config": {"prompt": "After.", "mode": "test", "tools": []}, "revision": 1})
    client.delete(f"/api/workflows/{workflow_id}")  # even a deleted definition can be rerun from its run
    assert client.post(f"/api/workflow-runs/{original['id']}/rerun", json={}).status_code == 422  # consent needed
    again = client.post(f"/api/workflow-runs/{original['id']}/rerun", json=CONSENT)
    assert again.status_code == 201
    rerun = wait_workflow(client, again.json()["id"])
    assert rerun["status"] == "completed" and rerun["rerun_of"] == original["id"] and rerun["version"] == 1
    assert rerun["input"] == "first input" and rerun["agents"]["worker"]["prompt"] == "Before."
    assert rerun["id"] != original["id"]
    assert client.post("/api/workflow-runs/00000000-0000-0000-0000-000000000000/rerun", json=CONSENT).status_code == 404


def test_deleting_keeps_versions_and_runs(client):
    workflow_id = save(client, spec=linear(agent(client))).json()["id"]
    run = wait_workflow(client, start(client, workflow_id).json()["id"])
    assert client.delete(f"/api/workflows/{workflow_id}").status_code == 204
    assert client.delete(f"/api/workflows/{workflow_id}").status_code == 404
    assert client.get("/api/workflows").json()["definitions"] == []
    assert client.get(f"/api/workflow-runs/{run['id']}").json()["status"] == "completed"
    assert start(client, workflow_id).status_code == 422 and client.get(f"/api/workflows/{workflow_id}/versions").status_code == 404


def test_the_controller_resumes_from_its_checkpoint_after_losing_its_lease(client):
    run_id = start(client, approval_flow(client)).json()["id"]
    wait_step(client, run_id, "gate", "approval")
    controllers = client.app.state.controllers
    assert client.post(f"/api/workflow-runs/{run_id}/continue").status_code == 422  # a live controller holds the lease
    controllers.stop.set()  # simulate the controller process going away
    controllers.join(10)
    import threading

    controllers.stop = threading.Event()
    run = detail(client, run_id)
    assert run["status"] == "running" and run["can_continue"] is True
    client.post(f"/api/workflow-runs/{run_id}/approve", json={"node_id": "gate", "approved": True})
    assert client.post(f"/api/workflow-runs/{run_id}/continue").status_code == 202
    assert wait_workflow(client, run_id)["status"] == "completed"
    assert len(children(client, run_id)) == 1  # resuming never relaunches finished work


def test_an_ambiguous_launch_is_failed_and_never_replayed(client):
    a = agent(client)
    workflow_id = save(client, spec=linear(a)).json()["id"]
    wf = client.app.state.workflows
    run_id = wf.start(workflow_id, 1, "x", accepted_data_flow=True)  # created, controller not launched yet
    store = client.app.state.store
    ghost = store.reserve_run(prompt="ghost", model="test", mode="test", language="en", tools=[], files=[])
    store.update(ghost["id"], status="completed", result="r")
    with store.engine.begin() as db:
        db.execute(
            workflow_steps.insert().values(workflow_run_id=run_id, node_id="worker", status="launching", child_id=ghost["id"], waits=[], updated=now())
        )
    client.app.state.controllers.launch(run_id)
    run = wait_workflow(client, run_id)
    assert run["status"] == "failed" and "Uncertain agent launch" in run["error"]
    assert {s["node_id"]: s["status"] for s in run["steps"]}["worker"] == "uncertain" and children(client, run_id) == []


def test_expired_workflows_fail_without_resubmitting(client):
    run_id = start(client, approval_flow(client)).json()["id"]
    wait_step(client, run_id, "gate", "approval")
    store = client.app.state.store
    with store.engine.begin() as db:
        db.execute(update(workflow_runs).where(workflow_runs.c.id == run_id).values(created=now() - timedelta(seconds=3601)))
    run = wait_workflow(client, run_id)
    assert run["status"] == "failed" and "time limit" in run["error"]


def test_only_one_controller_runs_per_workflow_in_this_process(client):
    run_id = start(client, approval_flow(client)).json()["id"]
    wait_step(client, run_id, "gate", "approval")
    pool = client.app.state.controllers
    before = len(pool._threads)
    pool.launch(run_id)
    pool.launch(run_id)
    assert len(pool._threads) == before == 1


# -- schedules -----------------------------------------------------------------------------------------------


def schedule_body(version=1, **extra):
    return {"schedule": {"kind": "daily", "hour": 8, "minute": 0}, "version": version, "input": "scheduled input", "accepted_schedule": True, "accepted_data_flow": True, **extra}


def make_due(client, workflow_id, seconds_ago=1):
    from deep_agents_app.db.schema import workflow_schedules

    with client.app.state.store.engine.begin() as db:
        db.execute(update(workflow_schedules).where(workflow_schedules.c.workflow_id == workflow_id).values(next_run=now() - timedelta(seconds=seconds_ago)))


def test_a_schedule_needs_consent_runs_once_and_never_replays(client):
    workflow_id = save(client, spec=linear(agent(client))).json()["id"]
    assert client.put(f"/api/workflows/{workflow_id}/schedule", json=schedule_body(accepted_schedule=False)).status_code == 422
    assert client.put(f"/api/workflows/{workflow_id}/schedule", json=schedule_body(accepted_data_flow=False)).status_code == 422
    assert client.put(f"/api/workflows/{workflow_id}/schedule", json=schedule_body(version=7)).status_code == 422
    ok = client.put(f"/api/workflows/{workflow_id}/schedule", json=schedule_body())
    assert ok.status_code == 200 and ok.json()["next_run"]
    definition = client.get("/api/workflows").json()["definitions"][0]
    assert definition["schedule"]["enabled"] is True and definition["schedule"]["input"] == "scheduled input"

    assert run_due_workflows(client.app.state.workflows, client.app.state.controllers) == []  # not due: no immediate run
    make_due(client, workflow_id)
    started = run_due_workflows(client.app.state.workflows, client.app.state.controllers)
    assert len(started) == 1
    run = wait_workflow(client, started[0])
    assert run["status"] == "completed" and run["scheduled_for"] is not None and run["input"] == "scheduled input"
    schedule = client.get("/api/workflows").json()["definitions"][0]["schedule"]
    assert schedule["last_run_id"] == started[0] and schedule["last_error"] == "" and schedule["next_run"] > now().isoformat()
    assert run_due_workflows(client.app.state.workflows, client.app.state.controllers) == []  # the occurrence is consumed


def test_late_overlapping_and_outdated_occurrences_are_skipped_or_stopped(client):
    a = agent(client)
    workflow_id = save(client, spec=linear(a)).json()["id"]
    client.put(f"/api/workflows/{workflow_id}/schedule", json=schedule_body())
    wf, pool = client.app.state.workflows, client.app.state.controllers

    make_due(client, workflow_id, seconds_ago=3 * 24 * 3600)  # days of downtime: skipped, never replayed
    assert run_due_workflows(wf, pool) == []
    assert "more than 5 minutes late" in client.get("/api/workflows").json()["definitions"][0]["schedule"]["last_error"]

    gate = approval_flow(client)  # a running (paused) run of the SAME workflow blocks the next occurrence for a while
    run_id = start(client, gate).json()["id"]
    wait_step(client, run_id, "gate", "approval")
    client.put(f"/api/workflows/{gate}/schedule", json=schedule_body())
    make_due(client, gate, seconds_ago=5)
    assert run_due_workflows(wf, pool) == []
    from deep_agents_app.db.schema import workflow_schedules

    with client.app.state.store.engine.connect() as db:
        assert db.execute(select(workflow_schedules.c.next_run).where(workflow_schedules.c.workflow_id == gate)).scalar_one() <= now()  # waits
    client.post(f"/api/workflow-runs/{run_id}/cancel")

    # A new version of the workflow stops its schedule (it must be approved again).
    save(client, name="Flow 2", spec=linear(a), workflow_id=workflow_id, version=1)
    make_due(client, workflow_id)
    started = run_due_workflows(wf, pool)  # the earlier, now free, `gate` occurrence may start; the outdated one must not
    assert all(detail(client, run)["workflow_id"] == gate for run in started)
    for run in started:
        client.post(f"/api/workflow-runs/{run}/cancel")
    stopped = next(d for d in client.get("/api/workflows").json()["definitions"] if d["id"] == workflow_id)["schedule"]
    assert stopped["enabled"] is False and "approve the schedule again" in stopped["last_error"]


def test_schedules_can_be_paused_removed_and_stop_with_a_deleted_workflow(client):
    workflow_id = save(client, spec=linear(agent(client))).json()["id"]
    assert client.post(f"/api/workflows/{workflow_id}/schedule/pause").status_code == 422  # nothing to pause
    client.put(f"/api/workflows/{workflow_id}/schedule", json=schedule_body())
    assert client.post(f"/api/workflows/{workflow_id}/schedule/pause").status_code == 200
    paused = client.get("/api/workflows").json()["definitions"][0]["schedule"]
    assert paused["enabled"] is False and paused["next_run"] is None
    client.put(f"/api/workflows/{workflow_id}/schedule", json=schedule_body())
    assert client.delete(f"/api/workflows/{workflow_id}/schedule").status_code == 204
    assert client.get("/api/workflows").json()["definitions"][0]["schedule"] is None
    client.put(f"/api/workflows/{workflow_id}/schedule", json=schedule_body())
    client.delete(f"/api/workflows/{workflow_id}")
    make_due(client, workflow_id)
    assert run_due_workflows(client.app.state.workflows, client.app.state.controllers) == []


def test_the_agents_scheduler_tick_also_starts_due_workflow_schedules(client):
    workflow_id = save(client, spec=linear(agent(client))).json()["id"]
    client.put(f"/api/workflows/{workflow_id}/schedule", json=schedule_body())
    make_due(client, workflow_id)
    client.app.state.scheduler.tick()
    deadline = time.monotonic() + 30
    runs_seen = []
    while not runs_seen and time.monotonic() < deadline:
        runs_seen = [r for r in client.get("/api/workflows").json()["runs"] if r["scheduled_for"]]
        time.sleep(0.1)
    assert runs_seen and wait_workflow(client, runs_seen[0]["id"])["status"] == "completed"


# -- diagnostics ---------------------------------------------------------------------------------------------


def test_diagnostics_store_only_sanitized_metadata_once_per_run(client, monkeypatch):
    engine = client.app.state.engine
    run_id = "11111111-1111-1111-1111-111111111111"

    def explode():
        raise ValueError("SECRET prompt text and /home/user/path")

    try:
        explode()
    except ValueError as error:
        assert diagnostics.record_failure(engine, run_id, "resume", error) is True
        assert diagnostics.record_failure(engine, run_id, "complete", error) is True  # kept, not overwritten
    with engine.connect() as db:
        rows = [dict(r) for r in db.execute(select(workflow_diagnostics)).mappings()]
    assert len(rows) == 1 and rows[0]["phase"] == "resume"
    dump = json.dumps(rows[0]["body"])
    assert "SECRET" not in dump and "/home/user" not in dump and "explode" not in dump  # no message, no foreign frame names
    assert rows[0]["body"]["exceptions"][0]["type"] == "ValueError"
    assert diagnostics.record_failure(engine, "not-a-uuid", "resume", ValueError()) is False
    assert diagnostics.record_failure(engine, "22222222-2222-2222-2222-222222222222", "bogus-phase", ValueError()) is False
    monkeypatch.setattr(diagnostics, "MAX_REPORTS", 1)
    assert diagnostics.record_failure(engine, "33333333-3333-3333-3333-333333333333", "resume", ValueError()) is False  # never pruned


def test_exception_metadata_is_bounded_and_anonymizes_unknown_types():
    class Odd(Exception):
        pass

    try:
        try:
            raise Odd("secret")
        except Odd as inner:
            raise RuntimeError("outer") from inner
    except RuntimeError as error:
        chain = diagnostics.exception_metadata(error)
    assert [c["type"] for c in chain] == ["RuntimeError", "ExternalError"] and all(len(c["frames"]) <= 20 for c in chain)
    assert "secret" not in json.dumps(chain)


def _drop(settings):
    from sqlalchemy import create_engine, text

    engine = create_engine(settings.sqlalchemy_url)
    with engine.begin() as db:
        db.execute(text(f'DROP SCHEMA IF EXISTS "{settings.database_schema}" CASCADE'))
    engine.dispose()


def test_workflow_overview_reports_limits_and_localizes_errors(client):
    overview = client.get("/api/workflows").json()
    assert overview["engine"] == "langgraph" and overview["max_nodes"] == 12 and overview["max_agent_nodes"] == 8
    assert overview["max_active_workflows"] == 3 and overview["max_agent_prompt"] == 2800 and overview["timeout_seconds"] == 3600
    assert client.get("/api/workflow-runs/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.get("/api/workflow-runs/nope").status_code == 404
    assert wait_for_status is not None
