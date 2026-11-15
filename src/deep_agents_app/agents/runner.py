"""Execute one run: build the Deep Agent, stream it, persist events and the result."""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

from ..config import Settings
from ..db.migrate import checkpointer_connection
from ..i18n import system_prompt
from ..llm.gateway import GatewayNotConfigured, ModelNotConfigured, build_model, build_test_model
from ..runs.errors import classify_error
from ..runs.events import EventBuffer
from ..runs.metrics import MetricsCallback
from ..runs.outcome import MAX_TOOL_FAILURES, assess_outcome, validation_failures
from ..runs.store import EventLimit, RunStore
from ..tools.registry import ToolContext, build_tools

log = logging.getLogger(__name__)
# Prefixes removed from a run's environment (shared with the worker launcher).
ENV_PREFIXES_REMOVED = ("LANGSMITH", "LANGCHAIN", "OPENAI", "ANTHROPIC", "GOOGLE", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
RECURSION_LIMIT = 1000
MAX_GRAPH_CONCURRENCY = 2  # streaming needs a spare executor waiter; 1 would deadlock
CANCEL_POLL_SECONDS = 1.0


def build_agent(settings: Settings, run: dict[str, Any], store: RunStore, saver: Any) -> Any:
    from deepagents import create_deep_agent
    from deepagents.backends import StateBackend
    from langchain.agents.middleware import TodoListMiddleware

    if run["mode"] == "test":
        model = build_test_model(run["prompt"])
    else:
        model = build_model(settings, run["model"], run["id"])
    context = ToolContext(settings, store, run["id"], run["language"], list(run["files"]))
    return create_deep_agent(
        model=model,
        backend=StateBackend(),
        tools=build_tools(list(run["tools"]), context),
        checkpointer=saver,
        middleware=[TodoListMiddleware()],
        system_prompt=system_prompt(run["language"]),
    )


def execute_run(settings: Settings, store: RunStore, run_id: str) -> None:
    from langgraph.checkpoint.postgres import PostgresSaver

    # No tracing, provider keys/base-URL overrides or proxies from the process environment may influence the
    # run (as in the previous system): the gateway is configured only through our own settings.
    for key in list(os.environ):
        if key.upper().startswith(ENV_PREFIXES_REMOVED):
            del os.environ[key]
    os.environ["LANGSMITH_TRACING"] = "false"

    run = store.get(run_id)
    if not run or run["status"] != "running":
        return
    created: datetime = run["created"]
    buffer = EventBuffer(lambda event, data: store.append_event(run_id, event, data))
    try:
        with checkpointer_connection(settings) as connection:
            agent = build_agent(settings, run, store, PostgresSaver(connection))  # type: ignore[arg-type]
            config = {
                "configurable": {"thread_id": run_id},
                "recursion_limit": RECURSION_LIMIT,
                "max_concurrency": MAX_GRAPH_CONCURRENCY,
                # Content-free call timing/token metrics; the callback can never alter or fail the run.
                "callbacks": [MetricsCallback(store, run_id, run["model"])],
            }
            store.update(run_id, graph=agent.get_graph().to_json())
            store.append_event(run_id, "metadata", {"run_id": run_id, "thread_id": run_id})
            input_value = {"messages": [{"role": "user", "id": run_id + "-prompt", "content": run["prompt"]}]}
            steps, last_check = 0, time.monotonic()
            for namespace, mode, event in agent.stream(
                input_value, config=config, stream_mode=["values", "messages", "updates", "custom"], subgraphs=True
            ):
                buffer.append(mode + ("|" + "|".join(namespace) if namespace else ""), event)
                if mode == "values" and not namespace:
                    if validation_failures(event.get("messages", [])) >= MAX_TOOL_FAILURES:
                        buffer.flush()
                        store.update(run_id, status="failed", outcome_status="blocked", error="run_blocked_tool_errors")
                        return
                    steps += 1
                    store.update(run_id, steps=steps)
                if time.monotonic() - last_check >= CANCEL_POLL_SECONDS:
                    last_check = time.monotonic()
                    if not store.is_active(run_id):
                        return  # cancelled (or reaped): stop quietly, the status is already final
                    if (datetime.now(UTC) - created).total_seconds() > settings.run_timeout_seconds:
                        store.update(run_id, status="failed", error="run_timeout")
                        return
            buffer.flush()
            messages = agent.get_state(config).values.get("messages", [])
            text = str(messages[-1].text) if messages else ""
            if not text:
                store.update(run_id, status="failed", error="error_empty_result")
            else:
                outcome = assess_outcome(text, messages, store.audit_errors(run_id))
                store.update(run_id, status="completed", result=text[:64000], outcome_status=outcome)
    except (GatewayNotConfigured, ModelNotConfigured):
        store.update(run_id, status="failed", error="gateway_not_configured")
    except EventLimit:
        store.update(run_id, status="failed", error="run_failed")
    except Exception as error:
        log.warning("run %s failed: %s", run_id, type(error).__name__)  # class only: never message/content
        store.update(run_id, status="failed", error=classify_error(error))
    finally:
        try:
            buffer.flush()
        except Exception:  # noqa: S110 - the run's final status is already recorded
            pass
