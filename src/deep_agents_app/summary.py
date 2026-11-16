"""Content-free "what happened today" summary (ported from the previous system's ``summary_support``).

Counts, outcomes, owner-facing failure causes and token totals for the current calendar day in
``SCHEDULE_TIMEZONE``; never prompts, results or tool data. A workflow problem shows the agent's own
stated ``outcome.reason`` (truncated) or the workflow's stored error key.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, func, select

from .db.schema import llm_calls, runs, workflow_runs
from .runs.store import now
from .saved.limits import RATE_LIMITED, limit_pause_until

PROBLEM_LIMIT = 10
_BAD = ("blocked", "error")


def day_start(moment: datetime, zone: ZoneInfo) -> datetime:
    local = moment.astimezone(zone)
    return datetime(local.year, local.month, local.day, tzinfo=zone)


def _reason(result: str) -> str:
    """The agent's own outcome.reason, truncated; anything else is not shown."""
    try:
        value = json.loads(result or "null")
    except ValueError:
        return ""
    outcome = value.get("outcome") if isinstance(value, dict) else None
    reason = outcome.get("reason") if isinstance(outcome, dict) else ""
    return reason[:300] if isinstance(reason, str) else ""


def daily_summary(engine: Engine, zone: ZoneInfo, cooldown_seconds: int, moment: datetime | None = None) -> dict[str, Any]:
    moment = moment or now()
    since = day_start(moment, zone)
    standalone = runs.c.workflow_run_id.is_(None)
    # A run the owner cancelled is not a problem (fail_run stamps such runs with outcome "error").
    problem = (workflow_runs.c.status == "failed") | (workflow_runs.c.outcome_status.in_(_BAD) & (workflow_runs.c.status != "cancelled"))

    def counts(column: Any, table: Any, *where: Any) -> dict[str, int]:
        query = select(column, func.count()).where(*where).group_by(column)
        return {key or "unknown": count for key, count in db.execute(query)}

    with engine.connect() as db:
        agent_status = counts(runs.c.status, runs, runs.c.created >= since, standalone)
        agent_outcome = counts(runs.c.outcome_status, runs, runs.c.created >= since, standalone, runs.c.status == "completed")
        flow_status = counts(workflow_runs.c.status, workflow_runs, workflow_runs.c.created >= since)
        flow_outcome = counts(workflow_runs.c.outcome_status, workflow_runs, workflow_runs.c.created >= since)
        problem_count = db.execute(select(func.count()).select_from(workflow_runs).where(workflow_runs.c.created >= since, problem)).scalar_one()
        problems = []
        rows = db.execute(
            select(workflow_runs.c.id, workflow_runs.c.name, workflow_runs.c.status, workflow_runs.c.outcome_status, workflow_runs.c.error, workflow_runs.c.updated)
            .where(workflow_runs.c.created >= since, problem).order_by(workflow_runs.c.updated.desc()).limit(PROBLEM_LIMIT)
        ).mappings().all()
        for row in rows:
            child = db.execute(
                select(runs.c.result).where(runs.c.workflow_run_id == row["id"], runs.c.outcome_status.in_(_BAD)).order_by(runs.c.updated.desc()).limit(1)
            ).scalar_one_or_none()
            problems.append({
                "id": row["id"], "name": row["name"], "status": row["status"], "outcome": row["outcome_status"] or "unknown",
                "reason": (_reason(child) if child else "") or "", "error": row["error"], "updated": row["updated"].isoformat(),
            })
        usage = db.execute(
            select(
                func.count().label("calls"), func.coalesce(func.sum(llm_calls.c.input_tokens), 0).label("input"),
                func.coalesce(func.sum(llm_calls.c.output_tokens), 0).label("output"),
                func.coalesce(func.sum(llm_calls.c.cache_read_tokens), 0).label("cache"),
                func.count().filter(llm_calls.c.status != "ok").label("errors"),
            ).where(llm_calls.c.started >= since)
        ).mappings().one()
        limited = db.execute(select(func.count()).select_from(llm_calls).where(llm_calls.c.started >= since, llm_calls.c.error == RATE_LIMITED)).scalar_one()
        paused = limit_pause_until(db, moment, cooldown_seconds)
    return {
        "since": since.isoformat(), "now": moment.isoformat(),
        "agents": {"total": sum(agent_status.values()), "by_status": agent_status, "by_outcome": agent_outcome},
        "workflows": {"total": sum(flow_status.values()), "by_status": flow_status, "by_outcome": flow_outcome, "problem_count": problem_count, "problems": problems},
        "tokens": {
            "calls": usage["calls"], "input": int(usage["input"]), "output": int(usage["output"]), "cache": int(usage["cache"]),
            "error_calls": usage["errors"], "cache_rate": round(usage["cache"] / usage["input"], 4) if usage["input"] else None,
        },
        "limit": {"rate_limited_calls": limited, "paused_until": paused.isoformat() if paused else None},
    }
