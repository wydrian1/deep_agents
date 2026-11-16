"""Workflow schedules share the agents' scheduler tick (same transaction rules, same miss/overlap/pause policy)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from ..runs.store import DiskLow
from .engine import ControllerPool
from .spec import WorkflowRejected
from .store import WorkflowStore

log = logging.getLogger(__name__)


def resume_orphans(store: WorkflowStore, pool: ControllerPool) -> list[str]:
    """Relaunch controllers whose lease expired (the previous one died). Same checkpoint, same children."""
    resumed: list[str] = []
    for run_id in store.orphaned_runs():
        try:
            pool.launch(run_id)
            resumed.append(run_id)
        except Exception as problem:
            log.warning("workflow resume failed: %s", type(problem).__name__)
    return resumed


def run_due_workflows(store: WorkflowStore, pool: ControllerPool, moment: datetime | None = None) -> list[str]:
    """Claim due occurrences (committed together with their advancement), then start them. Never retried."""
    moment = (moment or datetime.now(UTC)).astimezone(UTC)
    started: list[str] = []
    for start, due in store.claim_due(moment):
        run_id: str | None = None
        error = ""
        try:
            run_id = store.start(start["id"], start["version"], start["input"], accepted_data_flow=True, scheduled_for=due)
            pool.launch(run_id)
            started.append(run_id)
        except WorkflowRejected as problem:
            error = problem.key
        except DiskLow:
            error = "disk_low"
        except Exception as problem:
            log.warning("scheduled workflow start failed: %s", type(problem).__name__)
            error = "schedule_start_failed"
        store.record_scheduled_start(start["id"], run_id, error)
    return started
