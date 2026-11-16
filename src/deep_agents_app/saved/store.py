"""Saved agents: stored run configurations with an optional schedule.

Rules (all enforced here, in one place):

* An enabled schedule computes its first run from *now*: no immediate and no retroactive run.
* The scheduler claims due agents with ``FOR UPDATE SKIP LOCKED``, creates the run and advances
  ``next_run`` in ONE transaction, so several replicas never start the same occurrence twice.
* An occurrence older than ``MISSED_GRACE_SECONDS`` is skipped (``schedule_missed``), never replayed.
  An agent that still has an active run, or a full slot table, makes the occurrence wait (at most that
  grace period) and then it counts as missed. This matches the previous system.
* After a rate-limited model call, scheduled starts are paused for a while (``schedule_paused``, see limits.py).
* A failed scheduled run is never retried; the next occurrence is simply the next one.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..config import Settings
from ..db.schema import saved_agents, scheduler_state
from ..llm.gateway import resolve_model_name
from ..runs.store import Busy, RunStore, now
from .limits import limit_pause_until
from .schedule import Schedule, next_occurrence

log = logging.getLogger(__name__)
MAX_AGENTS = 100
MISSED_GRACE_SECONDS = 300
DUE_BATCH = 20


class AgentLimit(Exception):
    pass


class AgentNotFound(Exception):
    pass


class RevisionConflict(Exception):
    pass


class FilesMissing(Exception):
    pass


def start_values(settings: Settings, config: dict[str, Any]) -> dict[str, Any]:
    """Keyword arguments for ``RunStore.reserve_run`` from a stored/validated config.

    The model is resolved at start time, so a changed DEFAULT_MODEL applies to existing agents.
    Raises ModelNotConfigured when a live run has no model.
    """
    mode = config["mode"]
    model = "test" if mode == "test" else resolve_model_name(settings, config.get("model"))
    return {
        "prompt": config["prompt"], "model": model, "mode": mode, "language": config["language"],
        "tools": list(config["tools"]), "files": list(config["files"]),
    }


class SavedAgentStore:
    def __init__(self, runs: RunStore, settings: Settings):
        self.runs, self.settings, self.engine = runs, settings, runs.engine
        self.tz = ZoneInfo(settings.schedule_timezone)

    # -- CRUD ---------------------------------------------------------------------------

    def create(self, *, name: str, config: dict[str, Any], schedule: Schedule | None, enabled: bool) -> dict[str, Any]:
        agent_id, created = str(uuid.uuid4()), now()
        with self.engine.begin() as db:
            if db.execute(select(func.count()).select_from(saved_agents)).scalar_one() >= MAX_AGENTS:
                raise AgentLimit
            db.execute(
                insert(saved_agents).values(
                    id=agent_id, name=name, config=config,
                    schedule=schedule.canonical() if schedule else None, enabled=enabled,
                    next_run=self._first_run(schedule, enabled, created), created=created, updated=created,
                )
            )
        return self.get(agent_id)  # type: ignore[return-value]

    def get(self, agent_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as db:
            row = db.execute(select(saved_agents).where(saved_agents.c.id == agent_id)).mappings().first()
        return dict(row) if row else None

    def list_agents(self) -> list[dict[str, Any]]:
        with self.engine.connect() as db:
            return [dict(r) for r in db.execute(select(saved_agents).order_by(saved_agents.c.created.desc())).mappings()]

    def update(
        self, agent_id: str, *, revision: int, name: str, config: dict[str, Any], schedule: Schedule | None, enabled: bool
    ) -> dict[str, Any]:
        """Optimistic update: a stale revision never overwrites someone else's change."""
        moment = now()
        canonical = schedule.canonical() if schedule else None
        with self.engine.begin() as db:
            row = db.execute(select(saved_agents).where(saved_agents.c.id == agent_id).with_for_update()).mappings().first()
            if row is None:
                raise AgentNotFound
            if row["revision"] != revision:
                raise RevisionConflict
            unchanged_rule = row["enabled"] and enabled and row["schedule"] == canonical and row["next_run"] is not None
            next_run = row["next_run"] if unchanged_rule else self._first_run(schedule, enabled, moment)
            db.execute(
                update(saved_agents).where(saved_agents.c.id == agent_id).values(
                    name=name, config=config, schedule=canonical, enabled=enabled, next_run=next_run,
                    last_error="", revision=row["revision"] + 1, updated=moment,
                )
            )
            # Workflows that use this agent get a new version in the SAME transaction (all or nothing).
            from ..workflows.store import WorkflowStore

            updated_workflows = WorkflowStore.propagate_agent_edit(db, agent_id, name, config)
        agent = self.get(agent_id)
        assert agent is not None
        return {**agent, "updated_workflows": updated_workflows}

    def set_enabled(self, agent_id: str, enabled: bool) -> dict[str, Any]:
        """Turn the saved schedule on or off without editing the agent (the previous system's ``enable_saved``).

        Enabling computes the next run from *now* (no catch-up), clears the last error and bumps the revision.
        Raises AgentNotFound, or ValueError when enabling an agent that has no schedule.
        """
        moment = now()
        with self.engine.begin() as db:
            row = db.execute(select(saved_agents).where(saved_agents.c.id == agent_id).with_for_update()).mappings().first()
            if row is None:
                raise AgentNotFound
            schedule = Schedule(**row["schedule"]) if row["schedule"] else None
            if enabled and schedule is None:
                raise ValueError("no schedule")
            db.execute(
                update(saved_agents).where(saved_agents.c.id == agent_id).values(
                    enabled=enabled, next_run=self._first_run(schedule, enabled, moment), last_error="",
                    revision=row["revision"] + 1, updated=moment,
                )
            )
        return self.get(agent_id)  # type: ignore[return-value]

    def scheduler_status(self) -> dict[str, Any]:
        """Last scheduler heartbeat (any replica) and the end of an active rate-limit pause, if any."""
        with self.engine.connect() as db:
            heartbeat = db.execute(select(scheduler_state.c.heartbeat).where(scheduler_state.c.id == 1)).scalar_one_or_none()
            paused = limit_pause_until(db, now(), self.settings.rate_limit_cooldown_seconds)
        return {"heartbeat": heartbeat, "paused_until": paused}

    def delete(self, agent_id: str) -> bool:
        """Removes the agent and its future schedule. Existing runs, outputs and active work stay."""
        with self.engine.begin() as db:
            return db.execute(delete(saved_agents).where(saved_agents.c.id == agent_id)).rowcount == 1

    def _first_run(self, schedule: Schedule | None, enabled: bool, moment: datetime) -> datetime | None:
        return next_occurrence(schedule, moment, self.tz) if schedule and enabled else None

    # -- running ------------------------------------------------------------------------

    def _start_values(self, config: dict[str, Any]) -> dict[str, Any]:
        """Start arguments, after checking that every attached file still exists (as the previous system did)."""
        values = start_values(self.settings, config)
        if values["files"] and len(self.runs.get_input_files(values["files"])) != len(values["files"]):
            raise FilesMissing
        return values

    def start_now(self, agent_id: str) -> dict[str, Any]:
        """Manual run as a new run (new id, never resumes an old one).

        Raises AgentNotFound, Busy, ModelNotConfigured or FilesMissing. Like the previous system, it records
        ``last_run_id`` and clears the last scheduling error.
        """
        agent = self.get(agent_id)
        if agent is None:
            raise AgentNotFound
        values = self._start_values(agent["config"])
        with self.engine.begin() as db:
            run_id = self.runs.reserve_in(db, **values, agent_id=agent_id)
            db.execute(update(saved_agents).where(saved_agents.c.id == agent_id).values(last_run_id=run_id, last_error=""))
        return self.runs.get(run_id)  # type: ignore[return-value]

    def run_due(self, moment: datetime | None = None) -> list[str]:
        """Start every due occurrence (see module docstring). Returns the new run ids, to be submitted."""
        moment = (moment or datetime.now(UTC)).astimezone(UTC)
        started: list[str] = []
        with self.engine.begin() as db:
            # Heartbeat: lets /api/status show that some replica's scheduler is alive.
            db.execute(
                pg_insert(scheduler_state).values(id=1, heartbeat=moment).on_conflict_do_update(
                    index_elements=[scheduler_state.c.id], set_={"heartbeat": moment}
                )
            )
            rows = db.execute(
                select(saved_agents)
                .where(saved_agents.c.enabled.is_(True), saved_agents.c.next_run <= moment)
                .order_by(saved_agents.c.next_run)
                .limit(DUE_BATCH)
                .with_for_update(skip_locked=True)
            ).mappings().all()
            for row in rows:
                run_id = self._fire(db, dict(row), moment)
                if run_id:
                    started.append(run_id)
        return started

    def _fire(self, db: Any, agent: dict[str, Any], moment: datetime) -> str | None:
        schedule = Schedule(**agent["schedule"])
        due = agent["next_run"]
        upcoming = next_occurrence(schedule, moment, self.tz)

        def advance(error: str, run_id: str | None = None) -> None:
            db.execute(
                update(saved_agents).where(saved_agents.c.id == agent["id"]).values(
                    next_run=upcoming, last_error=error, updated=now(),
                    **({"last_run_id": run_id} if run_id else {}),
                )
            )

        if (moment - due).total_seconds() > MISSED_GRACE_SECONDS:
            advance("schedule_missed")
            return None
        if limit_pause_until(db, moment, self.settings.rate_limit_cooldown_seconds):
            advance("schedule_paused")  # consumed, never replayed; manual starts are not blocked
            return None
        if self.runs.has_active_run(db, agent["id"]):
            return None  # overlap: wait for the previous run, at most until the grace period ends
        try:
            values = self._start_values(agent["config"])
            with db.begin_nested():
                run_id = self.runs.reserve_in(db, **values, agent_id=agent["id"], scheduled_for=due)
        except Busy:
            return None  # no free slot: keep next_run, retried on the next tick until the grace period ends
        except Exception as error:
            log.warning("scheduled start of agent %s failed: %s", agent["id"], type(error).__name__)
            advance("schedule_start_failed")
            return None
        advance("", run_id)
        return run_id
