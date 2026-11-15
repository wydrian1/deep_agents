"""Tool registry: one place that declares what tools exist, what they may do, and what they need.

Add a tool by writing a factory and decorating it with :func:`register_tool`. Tools are
opt-in per run (``tools`` in the run request); nothing is enabled by default.

Every call is audited (tool, operation, outcome, duration; never arguments or content). The row is written
BEFORE the call as ``started``, so an interrupted call can never count as a success. All tools together are
limited to 200 calls per run (the previous system capped only its document/search/report tools; a runaway
web-fetch loop costs just as much).
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..i18n import message
from ..runs.store import RunStore

MAX_TOOL_CALLS_PER_RUN = 200


@dataclass
class ToolContext:
    settings: Settings
    store: RunStore
    run_id: str
    language: str
    file_ids: list[str] = field(default_factory=list)
    enabled: list[str] = field(default_factory=list)  # the run's tool grants

    def t(self, key: str, **values: object) -> str:
        return message(key, self.language, **values)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    label: str
    access: str  # "read" | "private-write"
    description: str
    factory: Callable[[ToolContext], list[Callable[..., Any]]]
    requires: tuple[str, ...] = ()


REGISTRY: dict[str, ToolSpec] = {}
Factory = Callable[[ToolContext], list[Callable[..., Any]]]


def register_tool(
    name: str, *, label: str, access: str, description: str, requires: tuple[str, ...] = ()
) -> Callable[[Factory], Factory]:
    def decorate(factory: Factory) -> Factory:
        REGISTRY[name] = ToolSpec(name, label, access, description, factory, requires)
        return factory

    return decorate


def load_builtin_tools() -> None:
    from . import documents, github, reports, web  # noqa: F401  (import registers the tools)


def catalog() -> list[dict[str, Any]]:
    load_builtin_tools()
    return [
        {"name": s.name, "label": s.label, "access": s.access, "description": s.description, "requires": list(s.requires)}
        for s in REGISTRY.values()
    ]


def validate_selection(names: list[str]) -> list[str]:
    """Return the missing/unknown tool names (empty list = valid)."""
    load_builtin_tools()
    problems = [n for n in names if n not in REGISTRY]
    for name in names:
        spec = REGISTRY.get(name)
        if spec:
            problems += [r for r in spec.requires if r not in names]
    return problems


def audited(ctx: ToolContext, tool_name: str, function: Callable[..., Any]) -> Callable[..., Any]:
    """Audit around a tool function: run still active, per-run call cap, started -> ok|error."""

    @functools.wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not ctx.store.is_active(ctx.run_id):
            return {"error": ctx.t("run_cancelled")}
        if ctx.store.tool_calls(ctx.run_id) >= MAX_TOOL_CALLS_PER_RUN:
            return {"error": ctx.t("tool_limit")}
        started = time.monotonic()
        audit_id = ctx.store.audit_start(ctx.run_id, tool_name, function.__name__)
        outcome = "error"
        try:
            result = function(*args, **kwargs)
            outcome = "error" if isinstance(result, dict) and "error" in result else "ok"
            return result
        except Exception:
            return {"error": ctx.t("tool_failed")}
        finally:
            ctx.store.audit_finish(audit_id, outcome, int((time.monotonic() - started) * 1000))

    return wrapper


def build_tools(names: list[str], ctx: ToolContext) -> list[Callable[..., Any]]:
    load_builtin_tools()
    ctx.enabled = list(names)
    tools: list[Callable[..., Any]] = []
    for name in names:
        spec = REGISTRY[name]
        tools.extend(audited(ctx, name, function) for function in spec.factory(ctx))
    return tools
