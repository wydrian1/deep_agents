"""Content-free model-call metrics.

A LangChain callback records, per model call: model name, status, duration and token counts
(input/output/cached/reasoning, when the gateway reports them). It never stores prompts,
completions or raw error text, and it can never change or fail a run.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from .errors import classify_error
from .store import RunStore

log = logging.getLogger(__name__)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def extract_usage(response: LLMResult) -> dict[str, int | None]:
    """Sum token usage over the response's generations (usage_metadata first, llm_output as a fallback)."""
    totals: dict[str, int | None] = {"input_tokens": None, "output_tokens": None, "cached_tokens": None, "reasoning_tokens": None}

    def add(key: str, value: Any) -> None:
        number = _int(value)
        if number is not None:
            totals[key] = (totals[key] or 0) + number

    found = False
    for generations in response.generations:
        for generation in generations:
            usage = getattr(getattr(generation, "message", None), "usage_metadata", None)
            if not isinstance(usage, dict):
                continue
            found = True
            add("input_tokens", usage.get("input_tokens"))
            add("output_tokens", usage.get("output_tokens"))
            add("cached_tokens", (usage.get("input_token_details") or {}).get("cache_read"))
            add("reasoning_tokens", (usage.get("output_token_details") or {}).get("reasoning"))
    if not found and isinstance(response.llm_output, dict):
        legacy = response.llm_output.get("token_usage") or {}
        add("input_tokens", legacy.get("prompt_tokens"))
        add("output_tokens", legacy.get("completion_tokens"))
    return totals


class MetricsCallback(BaseCallbackHandler):
    raise_error = False  # a metrics failure must never fail the run

    def __init__(self, store: RunStore, run_id: str, default_model: str):
        self.store, self.run_id, self.default_model = store, run_id, default_model
        self._calls: dict[UUID, tuple[float, datetime, str]] = {}

    def _begin(self, run_id: UUID, kwargs: dict[str, Any]) -> None:
        params = kwargs.get("invocation_params") or {}
        name = params.get("model") or params.get("model_name")
        model = name if isinstance(name, str) and name else self.default_model
        self._calls[run_id] = (time.monotonic(), datetime.now(UTC), model)

    def on_chat_model_start(self, serialized: dict[str, Any], messages: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._begin(run_id, kwargs)

    def on_llm_start(self, serialized: dict[str, Any], prompts: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._begin(run_id, kwargs)

    def _finish(self, run_id: UUID, status: str, error: str, usage: dict[str, int | None]) -> None:
        started = self._calls.pop(run_id, None)
        if started is None:
            return
        try:
            self.store.record_llm_call(
                run_id=self.run_id, call_id=str(run_id), model=started[2], status=status, error=error,
                started=started[1], duration_ms=int((time.monotonic() - started[0]) * 1000), **usage,
            )
        except Exception as problem:
            log.warning("metrics write failed: %s", type(problem).__name__)

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        try:
            usage = extract_usage(response)
        except Exception:
            usage = {"input_tokens": None, "output_tokens": None, "cached_tokens": None, "reasoning_tokens": None}
        self._finish(run_id, "ok", "", usage)

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._finish(
            run_id, "error", classify_error(error),
            {"input_tokens": None, "output_tokens": None, "cached_tokens": None, "reasoning_tokens": None},
        )
