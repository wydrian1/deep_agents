"""Bounded DAG specification (ported from the previous system's ``spec_input``).

2-12 nodes, at most 8 agent nodes and 24 edges; exactly one start and one end; at most 4 inputs and 3 outputs per
node; no cycles; every node lies on a path from start to end. Conditions are fixed text checks (contains / does not
contain / empty / non-empty), never code. Rejections carry a message key, localized by the caller.
"""

from __future__ import annotations

import re
from typing import Any

NODE_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
UUID = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
KINDS = {"start", "agent", "condition", "join", "approval", "end"}
OPERATORS = ("contains", "not_contains", "empty", "nonempty")
RESERVED = {"constructor", "prototype", "outputs"}
NODE_KEYS = {"id", "kind", "label", "x", "y", "agentId", "operator", "value"}
MAX_NODES, MAX_AGENTS, MAX_EDGES = 12, 8, 24


class WorkflowRejected(Exception):
    """A request that breaks a rule. ``key`` is a message key (localized by the caller)."""

    def __init__(self, key: str, **values: object):
        super().__init__(key)
        self.key, self.values = key, values


def _int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def valid_uuid(value: object) -> str:
    if not isinstance(value, str) or not UUID.match(value):
        raise WorkflowRejected("wf_not_found")
    return value


def validate_spec(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"version", "nodes", "edges"} or not _int(value["version"]) or value["version"] != 1:
        raise WorkflowRejected("wf_bad_graph")
    nodes, edges = value["nodes"], value["edges"]
    if not isinstance(nodes, list) or not 2 <= len(nodes) <= MAX_NODES or not isinstance(edges, list) or not 1 <= len(edges) <= MAX_EDGES:
        raise WorkflowRejected("wf_graph_size")
    by_id: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if not isinstance(node, dict) or set(node) != NODE_KEYS:
            raise WorkflowRejected("wf_bad_node")
        name, kind = node["id"], node["kind"]
        if not isinstance(name, str) or not NODE_ID.match(name) or name in RESERVED or name in by_id or not isinstance(kind, str) or kind not in KINDS:
            raise WorkflowRejected("wf_node_id")
        label = node["label"]
        if not isinstance(label, str) or not label.strip() or len(label) > 80 or any(ord(c) < 32 for c in label):
            raise WorkflowRejected("wf_node_label")
        if any(not _int(node[k]) or not 0 <= node[k] <= 2000 for k in ("x", "y")):
            raise WorkflowRejected("wf_node_position")
        if kind == "agent":
            valid_uuid_or(node["agentId"], "wf_agent_id")
        elif node["agentId"] != "":
            raise WorkflowRejected("wf_agent_only")
        if kind == "condition":
            if (
                node["operator"] not in OPERATORS or not isinstance(node["value"], str) or len(node["value"]) > 200
                or (node["operator"] in ("contains", "not_contains") and not node["value"])
            ):
                raise WorkflowRejected("wf_condition")
        elif node["operator"] != "" or node["value"] != "":
            raise WorkflowRejected("wf_condition_only")
        by_id[name] = dict(node)
    kinds = [n["kind"] for n in by_id.values()]
    if kinds.count("start") != 1 or kinds.count("end") != 1 or kinds.count("agent") > MAX_AGENTS:
        raise WorkflowRejected("wf_endpoints")
    incoming: dict[str, list[dict[str, Any]]] = {k: [] for k in by_id}
    outgoing: dict[str, list[dict[str, Any]]] = {k: [] for k in by_id}
    pairs: set[tuple[str, str]] = set()
    for edge in edges:
        if not isinstance(edge, dict) or set(edge) != {"source", "target", "branch"}:
            raise WorkflowRejected("wf_bad_edge")
        source, target, branch = edge["source"], edge["target"], edge["branch"]
        if (
            not isinstance(source, str) or not isinstance(target, str) or source not in by_id or target not in by_id
            or source == target or (source, target) in pairs or branch not in ("always", "true", "false")
        ):
            raise WorkflowRejected("wf_bad_edge")
        if (by_id[source]["kind"] == "condition") != (branch in ("true", "false")):
            raise WorkflowRejected("wf_branch")
        pairs.add((source, target))
        incoming[target].append(edge)
        outgoing[source].append(edge)
    for key, node in by_id.items():
        if (
            (node["kind"] == "start" and incoming[key]) or (node["kind"] != "start" and not incoming[key])
            or (node["kind"] == "end" and outgoing[key]) or (node["kind"] != "end" and not outgoing[key])
        ):
            raise WorkflowRejected("wf_connectivity")
        if (
            len(incoming[key]) > 4 or len(outgoing[key]) > 3
            or (node["kind"] == "condition" and (len(incoming[key]) != 1 or sorted(e["branch"] for e in outgoing[key]) != ["false", "true"]))
        ):
            raise WorkflowRejected("wf_degrees")
    degree = {key: len(items) for key, items in incoming.items()}
    queue = [key for key, count in degree.items() if not count]
    order: list[str] = []
    while queue:
        key = queue.pop(0)
        order.append(key)
        for edge in outgoing[key]:
            degree[edge["target"]] -= 1
            if degree[edge["target"]] == 0:
                queue.append(edge["target"])
    if len(order) != len(by_id):
        raise WorkflowRejected("wf_cycle")
    # One source, one sink and in/out edges on every other node: no disconnected component or dead-end branch.
    return {"version": 1, "nodes": [by_id[key] for key in order], "edges": [dict(e) for e in edges]}


def valid_uuid_or(value: object, key: str) -> str:
    if not isinstance(value, str) or not UUID.match(value):
        raise WorkflowRejected(key)
    return value


def unique_labels(spec: dict[str, Any]) -> None:
    """New versions need unique step names (case/space-insensitive); older saved graphs stay valid."""
    seen: set[str] = set()
    for node in spec["nodes"]:
        key = node["label"].strip().casefold()
        if key in seen:
            raise WorkflowRejected("wf_duplicate_label", label=node["label"])
        seen.add(key)
