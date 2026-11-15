"""Rate-limit pause for *scheduled* starts (ported from the previous system's ``limit_support``).

After the gateway answered a model call with a rate limit / exhausted budget (HTTP 429), every call
fails for a while, so each scheduled occurrence would only burn a failed run. Scheduled occurrences are
therefore skipped for ``RATE_LIMIT_COOLDOWN_SECONDS`` after the last rate-limited call, unless a model
call has succeeded since. This is a pause, not a retry: the skipped occurrence is consumed and never
replayed, and manual starts are never blocked. The cooldown is a conservative estimate; the real reset
time is unknown to us (LiteLLM budgets reset on their own schedule).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from ..db.schema import llm_calls

RATE_LIMITED = "error_rate_limited"


def limit_pause_until(db: Any, moment: datetime, cooldown_seconds: int) -> datetime | None:
    if cooldown_seconds <= 0:
        return None
    last = db.execute(select(func.max(llm_calls.c.started)).where(llm_calls.c.error == RATE_LIMITED)).scalar_one()
    if last is None:
        return None
    until = last + timedelta(seconds=cooldown_seconds)
    if until <= moment:
        return None
    recovered = db.execute(select(llm_calls.c.id).where(llm_calls.c.started > last, llm_calls.c.status == "ok").limit(1)).first()
    return None if recovered else until
