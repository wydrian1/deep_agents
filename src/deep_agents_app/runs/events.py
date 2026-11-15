"""JSON projection of LangGraph stream output and message-delta coalescing."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from typing import Any

from langchain_core.messages import AIMessageChunk, BaseMessage


def serializable(value: Any) -> Any:
    if isinstance(value, BaseMessage) or hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return serializable(asdict(value))
    if isinstance(value, dict):
        return {str(key): serializable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [serializable(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)[:2000]


class EventBuffer:
    """Coalesce message deltas; never tool/state events.

    Flushes on every non-message event, message identity change, 64 KiB, or 250 ms
    (checked at the next incoming chunk). No background thread.
    """

    def __init__(self, sink: Callable[[str, Any], object], clock: Callable[[], float] = time.monotonic):
        self.sink, self.clock = sink, clock
        self.pending: tuple[str, AIMessageChunk, dict] | None = None
        self.size, self.last_flush = 0, clock()

    def flush(self) -> None:
        if self.pending is not None:
            event, chunk, metadata = self.pending
            self.sink(event, (chunk, metadata))
            self.pending, self.size, self.last_flush = None, 0, self.clock()

    def append(self, event: str, data: Any) -> None:
        is_delta = (
            event.split("|")[0] == "messages"
            and isinstance(data, tuple | list)
            and len(data) == 2
            and isinstance(data[0], AIMessageChunk)
            and isinstance(data[1], dict)
        )
        if not is_delta:
            self.flush()
            self.sink(event, data)
            return
        chunk, metadata = data
        size = len(json.dumps(serializable(chunk), ensure_ascii=True))
        if self.pending is not None:
            old_event, old_chunk, old_metadata = self.pending
            if (event, chunk.id, metadata) != (old_event, old_chunk.id, old_metadata) or self.size + size > 65536:
                self.flush()
        if self.pending is None:
            self.pending = (event, chunk, metadata)
        else:
            try:
                self.pending = (event, self.pending[1] + chunk, metadata)
            except (TypeError, ValueError):
                self.flush()
                self.pending = (event, chunk, metadata)
        self.size += size
        if self.size >= 65536 or self.clock() - self.last_flush >= 0.25:
            self.flush()
