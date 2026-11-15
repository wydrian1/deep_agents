"""Business outcome is separate from technical completion; never inferred from prose.

Ported from the previous system's ``outcome_support`` (without its Gmail/parcel-specific profile):

* ``validation_failures`` counts failed tool results. Four of them stop the run (a model looping on a
  broken tool would otherwise spend its whole budget).
* ``assess_outcome``: a run whose tools failed, or returned partial/conflicting data, is ``blocked`` even though
  the run itself completed. An agent may state its own outcome as a JSON envelope
  ``{"outcome": {"status": "success|noop|blocked|error", "reason": "..."}}``; anything else is ``unknown``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

STATUSES = {"success", "noop", "blocked", "error"}
MAX_TOOL_FAILURES = 4


def tool_data(messages: list[Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    for message in messages:
        if getattr(message, "type", None) != "tool":
            continue
        content = getattr(message, "content", None)
        if not isinstance(content, str):
            continue
        try:
            value = json.loads(content)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            yield getattr(message, "name", "") or "", value


def validation_failures(messages: list[Any]) -> int:
    count = 0
    for message in messages:
        if getattr(message, "type", None) != "tool":
            continue
        if getattr(message, "status", None) == "error":
            count += 1
            continue
        for _name, value in tool_data([message]):
            if "error" in value or value.get("partial") is True:
                count += 1
    return count


def assess_outcome(text: str, messages: list[Any], audit_errors: int = 0) -> str:
    if audit_errors or validation_failures(messages):
        return "blocked"
    if any("error" in v or v.get("partial") is True or v.get("conflict") is True for _n, v in tool_data(messages)):
        return "blocked"
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return "unknown"
    if not isinstance(value, dict) or "outcome" not in value:
        return "unknown"
    outcome = value["outcome"]
    if (
        not isinstance(outcome, dict) or set(outcome) != {"status", "reason"} or outcome["status"] not in STATUSES
        or not isinstance(outcome["reason"], str) or len(outcome["reason"]) > 1000
    ):
        return "unknown"
    return str(outcome["status"])
