"""Studio graph projection: the real LangGraph topology plus normalized, bounded step events.

Port of the previous system's ``studioGraph``/``traceEvent``. Only normalized node ids, edges and short
event summaries are returned: no framework metadata, nothing is invented, and colours in the UI mean
"observed activity", never percentage progress.
"""

from __future__ import annotations

import json
from typing import Any

from ..runs.store import RunStore

MAX_NODES, MAX_EDGES, BATCH = 50, 150, 500
MAX_DETAIL = 6000


def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "".join(str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") == "text")
    else:
        text = ""
    if not text and message.get("tool_calls"):
        text = ", ".join(str(c.get("name", "")) for c in message["tool_calls"] if isinstance(c, dict))
    return text


def trace_event(row: dict[str, Any]) -> dict[str, Any] | None:
    mode, *namespace = str(row["event"]).split("|")
    if mode not in ("messages", "updates"):
        return None
    data = row["data"]
    metadata = data[1] if mode == "messages" and isinstance(data, list) and len(data) > 1 and isinstance(data[1], dict) else None
    node: Any = None
    if mode == "messages":
        node = metadata.get("langgraph_node") if metadata else None
    elif isinstance(data, dict):
        node = next((k for k in data if k != "__interrupt__"), None) or ("tools" if "__interrupt__" in data else None)
    if not isinstance(node, str) or not node or len(node) > 200:
        return None
    message = data[0] if mode == "messages" and isinstance(data, list) and data else None
    text = _message_text(message)
    interrupted = mode == "updates" and isinstance(data, dict) and bool(data.get("__interrupt__"))
    step = metadata.get("langgraph_step") if metadata else None
    return {
        "id": row["id"], "node": node, "namespace": "|".join(namespace)[:500],
        "kind": "interrupt" if interrupted else "message" if mode == "messages" else "update",
        "step": step if isinstance(step, int) and not isinstance(step, bool) else None,
        # Empty when there is nothing to say; the UI shows a localized default for the event kind.
        "summary": " ".join(text.split())[:180],
        "detail": json.dumps(data, indent=2, default=str)[:MAX_DETAIL],
    }


def studio_graph(store: RunStore, run_id: str, after: int) -> dict[str, Any] | None:
    run = store.get(run_id)
    if run is None:
        return None
    maximum = store.last_event_id(run_id)
    if after < 0 or after > maximum:
        raise ValueError("cursor")
    definition = run["graph"] if isinstance(run["graph"], dict) else {}
    raw_nodes: list[Any] = definition["nodes"] if isinstance(definition.get("nodes"), list) else []
    nodes = [
        {"id": n["id"]} for n in raw_nodes if isinstance(n, dict) and isinstance(n.get("id"), str) and len(n["id"]) <= 200
    ][:MAX_NODES]
    ids = {n["id"] for n in nodes}
    raw_edges: list[Any] = definition["edges"] if isinstance(definition.get("edges"), list) else []
    edges = [
        {"source": e["source"], "target": e["target"], "conditional": e.get("conditional") is True}
        for e in raw_edges if isinstance(e, dict) and e.get("source") in ids and e.get("target") in ids
    ][:MAX_EDGES]
    rows = store.trace_rows(run_id, after, BATCH + 1)
    has_more = len(rows) > BATCH
    batch = rows[:BATCH]
    events = [e for e in (trace_event(r) for r in batch) if e is not None]
    cursor = batch[-1]["id"] if has_more else maximum
    return {
        "nodes": nodes, "edges": edges, "coverage": "definition" if nodes else "observed",
        "events": events, "cursor": cursor, "hasMore": has_more,
    }
