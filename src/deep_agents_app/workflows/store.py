"""Workflow definitions, immutable versions, runs, steps and schedules on Postgres.

Ported rules (see docs/DIVERGENCES.md for the deliberate differences):

* Saving always creates a NEW immutable version and freezes the saved agents' configurations at that moment;
  the browser never supplies prompts or tool grants. Optimistic ``expected_version``; step names must be unique.
* Editing a saved agent publishes new versions of every non-deleted workflow that uses it, in the SAME
  transaction as the agent save (all or nothing).
* Starting needs explicit consent to pass private data between steps; a run pins its version, graph and agent
  snapshots, so later edits or deletions never change it. A rerun is a new instance of the original.
* At most 3 active workflows, 1,000 stored runs, 50 definitions and 500 versions; one hour for the whole run.
* A schedule (one per workflow) is pinned to a version and to the consents given; a newer version or a deletion
  stops it. Missed (> 5 minutes), overlapping or failed occurrences are never replayed or retried.
"""

from __future__ import annotations

import shutil
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..config import Settings
from ..db.schema import (
    runs,
    saved_agents,
    workflow_runs,
    workflow_schedules,
    workflow_steps,
    workflow_versions,
    workflows,
)
from ..runs.store import ACTIVE, DiskLow, RunStore, now
from ..saved.limits import limit_pause_until
from ..saved.schedule import Schedule, next_occurrence
from ..saved.store import MISSED_GRACE_SECONDS, start_values
from .spec import WorkflowRejected, unique_labels, validate_spec

MAX_ACTIVE_WORKFLOWS, MAX_STORED_RUNS, MAX_DEFINITIONS, MAX_VERSIONS = 3, 1000, 50, 500
MAX_AGENT_PROMPT = 2800  # leaves room for the passed-on input inside the 4,000-character prompt limit
MAX_COMPARED_VERSIONS = 30
LEASE_SECONDS = 120
_LOCK = 724_190_554


class WorkflowConflict(Exception):
    """The workflow changed; reload instead of overwriting."""


def _public_agents(agents: dict[str, Any]) -> dict[str, Any]:
    return {
        key: {
            "name": value["name"], "prompt": value["config"]["prompt"], "mode": value["config"]["mode"],
            "model": value["config"].get("model"), "tools": value["config"]["tools"], "files": value["config"].get("files", []),
        }
        for key, value in agents.items()
    }


def aggregate(summaries: list[dict[str, Any]], wall_ms: int) -> dict[str, Any]:
    """Combine child-run summaries; wall time is the workflow's own, not a sum (children can overlap)."""
    items = [s for s in summaries if s]

    def add(key: str) -> int | None:
        values = [s[key] for s in items if s[key] is not None]
        return sum(values) if values else None

    return {
        "wall_ms": wall_ms, "startup_ms": None, "model_ms": add("model_ms"), "tool_ms": add("tool_ms") or 0, "other_ms": None,
        "model_calls": sum(s["model_calls"] for s in items), "subagent_calls": sum(s["subagent_calls"] for s in items),
        "error_calls": sum(s["error_calls"] for s in items), "tool_calls": sum(s["tool_calls"] for s in items),
        "input_tokens": add("input_tokens"), "output_tokens": add("output_tokens"),
        "cache_read_tokens": add("cache_read_tokens"), "reasoning_tokens": add("reasoning_tokens"),
        "usage_calls": sum(s["usage_calls"] for s in items), "measured": any(s["measured"] for s in items),
    }


class WorkflowStore:
    def __init__(self, runs_store: RunStore, settings: Settings):
        self.runs, self.settings, self.engine = runs_store, settings, runs_store.engine

    # -- reads ---------------------------------------------------------------------------

    def definitions(self) -> list[dict[str, Any]]:
        with self.engine.connect() as db:
            schedules = {r["workflow_id"]: dict(r) for r in db.execute(select(workflow_schedules)).mappings()}
            rows = db.execute(
                select(
                    workflows.c.id, workflows.c.name, workflows.c.version, workflows.c.created, workflows.c.updated,
                    workflow_versions.c.spec, workflow_versions.c.agents,
                )
                .join(workflow_versions, (workflow_versions.c.workflow_id == workflows.c.id) & (workflow_versions.c.version == workflows.c.version))
                .where(workflows.c.deleted.is_(False))
                .order_by(workflows.c.updated.desc())
            ).mappings()
            result = []
            for row in rows:
                item = {k: row[k] for k in ("id", "name", "version", "created", "updated", "spec")}
                item["agents"] = _public_agents(row["agents"])
                item["schedule"] = self._public_schedule(schedules.get(row["id"]))
                result.append(item)
        return result

    @staticmethod
    def _public_schedule(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "schedule": row["schedule"], "enabled": row["enabled"], "version": row["version"], "input": row["input"],
            "next_run": row["next_run"], "last_run_id": row["last_run_id"], "last_error": row["last_error"],
        }

    def run_summaries(self, limit: int = 30) -> list[dict[str, Any]]:
        self.recover_expired()
        with self.engine.connect() as db:
            return [
                {k: r[k] for k in ("id", "workflow_id", "version", "name", "status", "outcome_status", "created", "updated", "error", "heartbeat", "rerun_of", "scheduled_for")}
                for r in db.execute(select(workflow_runs).order_by(workflow_runs.c.created.desc()).limit(limit)).mappings()
            ]

    def run_detail(self, run_id: str) -> dict[str, Any] | None:
        self.recover_expired()
        with self.engine.connect() as db:
            row = db.execute(select(workflow_runs).where(workflow_runs.c.id == run_id)).mappings().first()
            if row is None:
                return None
            steps = [
                dict(s) for s in db.execute(
                    select(workflow_steps).where(workflow_steps.c.workflow_run_id == run_id)
                    .order_by(workflow_steps.c.updated, workflow_steps.c.node_id)
                ).mappings()
            ]
            child_ids = [s["child_id"] for s in steps if s["child_id"]]
            children = (
                [dict(r) for r in db.execute(select(runs.c.id, runs.c.created, runs.c.updated, runs.c.status).where(runs.c.id.in_(child_ids))).mappings()]
                if child_ids else []
            )
        summaries = self.runs.summarize(children)
        child_rows = {c["id"]: c for c in children}
        moment = now()
        for step in steps:
            step.pop("waits", None)
            step.pop("workflow_run_id", None)
            step["metrics"] = summaries.get(step["child_id"]) if step["child_id"] else None
            child = child_rows.get(step["child_id"])
            # The child run's real timestamps; "finished" is unknown while it is still active.
            step["started"] = child["created"] if child else None
            step["finished"] = child["updated"] if child and child["status"] != ACTIVE else None
        end = moment if row["status"] == "running" else row["updated"]
        wall = max(0, round((end - row["created"]).total_seconds() * 1000))
        lease_until = row["lease_until"]
        detail = {k: row[k] for k in ("id", "workflow_id", "version", "name", "status", "outcome_status", "created", "updated", "error", "heartbeat", "rerun_of", "scheduled_for", "input", "spec")}
        detail["agents"] = _public_agents(row["agents"])
        detail["steps"] = steps
        detail["can_continue"] = row["status"] == "running" and (lease_until is None or lease_until < moment)
        detail["metrics"] = aggregate([s["metrics"] for s in steps if s["metrics"]], wall)
        return detail

    def versions(self, workflow_id: str) -> list[dict[str, Any]] | None:
        with self.engine.connect() as db:
            if db.execute(select(workflows.c.id).where(workflows.c.id == workflow_id, workflows.c.deleted.is_(False))).first() is None:
                return None
            rows = db.execute(
                select(workflow_versions).where(workflow_versions.c.workflow_id == workflow_id)
                .order_by(workflow_versions.c.version.desc()).limit(MAX_COMPARED_VERSIONS)
            ).mappings()
            return [
                {
                    "version": r["version"], "created": r["created"], "spec": r["spec"],
                    "agents": {k: {f: v[f] for f in ("name", "prompt", "mode", "tools")} for k, v in _public_agents(r["agents"]).items()},
                }
                for r in rows
            ]

    def origins(self, run_ids: list[str]) -> dict[str, dict[str, Any]]:
        """For agent runs that were workflow steps: the workflow's name and the step's name (for the run list)."""
        if not run_ids:
            return {}
        with self.engine.connect() as db:
            rows = db.execute(
                select(runs.c.id, runs.c.workflow_run_id, runs.c.workflow_node_id, workflow_runs.c.name, workflow_runs.c.spec)
                .join(workflow_runs, workflow_runs.c.id == runs.c.workflow_run_id)
                .where(runs.c.id.in_(run_ids))
            ).mappings()
            result = {}
            for r in rows:
                label = next((n["label"] for n in r["spec"]["nodes"] if n["id"] == r["workflow_node_id"]), "")
                result[r["id"]] = {"id": r["workflow_run_id"], "name": r["name"], "node": r["workflow_node_id"], "node_label": label}
            return result

    # -- saving --------------------------------------------------------------------------

    def snapshot_agents(self, db: Any, spec: dict[str, Any]) -> dict[str, Any]:
        snapshots: dict[str, Any] = {}
        for node in spec["nodes"]:
            if node["kind"] != "agent":
                continue
            row = db.execute(select(saved_agents.c.name, saved_agents.c.config).where(saved_agents.c.id == node["agentId"])).mappings().first()
            if row is None:
                raise WorkflowRejected("wf_agent_not_found")
            config = dict(row["config"])
            if len(config.get("prompt", "")) > MAX_AGENT_PROMPT:
                raise WorkflowRejected("wf_prompt_long", limit=MAX_AGENT_PROMPT)
            self.check_config(config)
            snapshots[node["id"]] = {"name": row["name"], "config": config}
        return snapshots

    def check_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """The same checks a manual agent start makes: tools exist, a model resolves, attached files still exist."""
        try:
            values = start_values(self.settings, config)
            if values["files"] and len(self.runs.get_input_files(values["files"])) != len(values["files"]):
                raise ValueError("files")
        except Exception:
            raise WorkflowRejected("wf_agent_invalid") from None
        return values

    def save(self, *, name: str, spec_input: object, expected_version: int, workflow_id: str | None) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise WorkflowRejected("wf_name")
        spec = validate_spec(spec_input)
        unique_labels(spec)
        if expected_version < 0:
            raise WorkflowRejected("wf_bad_version")
        new_id = workflow_id or str(uuid.uuid4())
        moment = now()
        with self.engine.begin() as db:
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK})
            agents = self.snapshot_agents(db, spec)
            row = db.execute(select(workflows.c.version, workflows.c.deleted).where(workflows.c.id == new_id).with_for_update()).mappings().first()
            if (row and (row["deleted"] or row["version"] != expected_version)) or (not row and (expected_version != 0 or workflow_id)):
                raise WorkflowConflict
            if db.execute(select(func.count()).select_from(workflow_versions)).scalar_one() >= MAX_VERSIONS or (
                not row and db.execute(select(func.count()).select_from(workflows).where(workflows.c.deleted.is_(False))).scalar_one() >= MAX_DEFINITIONS
            ):
                raise WorkflowRejected("wf_store_full")
            version = expected_version + 1
            if row:
                db.execute(update(workflows).where(workflows.c.id == new_id).values(name=name.strip(), version=version, updated=moment))
            else:
                db.execute(insert(workflows).values(id=new_id, name=name.strip(), version=version, created=moment, updated=moment))
            db.execute(insert(workflow_versions).values(workflow_id=new_id, version=version, spec=spec, agents=agents, created=moment))
        return {"id": new_id, "version": version}

    def delete(self, workflow_id: str) -> bool:
        """Logical delete: versions, runs and checkpoints stay; its schedule stops at the next occurrence."""
        with self.engine.begin() as db:
            return db.execute(
                update(workflows).where(workflows.c.id == workflow_id, workflows.c.deleted.is_(False)).values(deleted=True, updated=now())
            ).rowcount == 1

    # -- agent edits propagate to workflows ----------------------------------------------

    @staticmethod
    def propagate_agent_edit(db: Any, agent_id: str, name: str, config: dict[str, Any]) -> int:
        """New versions of every non-deleted workflow using the agent, in the caller's (agent save) transaction."""
        updates: list[tuple[Any, dict[str, Any]]] = []
        rows = db.execute(
            select(workflows.c.id, workflows.c.version, workflow_versions.c.spec, workflow_versions.c.agents)
            .join(workflow_versions, (workflow_versions.c.workflow_id == workflows.c.id) & (workflow_versions.c.version == workflows.c.version))
            .where(workflows.c.deleted.is_(False))
        ).mappings().all()
        for row in rows:
            agents = dict(row["agents"])
            affected = [n["id"] for n in row["spec"]["nodes"] if n["kind"] == "agent" and n["agentId"] == agent_id]
            if not affected:
                continue
            if len(config["prompt"]) > MAX_AGENT_PROMPT:
                raise WorkflowRejected("wf_agent_in_workflow_prompt", limit=MAX_AGENT_PROMPT)
            if all(agents[k] == {"name": name, "config": config} for k in affected):
                continue
            for k in affected:
                agents[k] = {"name": name, "config": config}
            updates.append((row, agents))
        if db.execute(select(func.count()).select_from(workflow_versions)).scalar_one() + len(updates) > MAX_VERSIONS:
            raise WorkflowRejected("wf_store_full_agent")
        moment = now()
        for row, agents in updates:
            version = row["version"] + 1
            db.execute(insert(workflow_versions).values(workflow_id=row["id"], version=version, spec=row["spec"], agents=agents, created=moment))
            db.execute(update(workflows).where(workflows.c.id == row["id"]).values(version=version, updated=moment))
        return len(updates)

    # -- running -------------------------------------------------------------------------

    def start(
        self, workflow_id: str, version: int, text_input: str, *, accepted_data_flow: bool,
        scheduled_for: datetime | None = None, rerun_of: str | None = None,
    ) -> str:
        """Create a running workflow row; the caller launches its controller. Raises WorkflowRejected."""
        if not accepted_data_flow:
            raise WorkflowRejected("wf_dataflow_required")
        if not isinstance(text_input, str) or len(text_input) > self.settings.max_prompt_chars:
            raise WorkflowRejected("wf_input_long", limit=self.settings.max_prompt_chars)
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.settings.data_dir).free < self.settings.min_free_disk_mb * 1024 * 1024:
            raise DiskLow
        self.recover_expired()
        with self.engine.connect() as db:
            if rerun_of:
                source = db.execute(select(workflow_runs).where(workflow_runs.c.id == rerun_of)).mappings().first()
                if source is None:
                    raise WorkflowRejected("wf_run_not_found")
                name, spec, agents, text_input, version = source["name"], source["spec"], source["agents"], source["input"], source["version"]
                workflow_id = source["workflow_id"]
            else:
                row = db.execute(
                    select(workflows.c.name, workflow_versions.c.spec, workflow_versions.c.agents)
                    .join(workflow_versions, (workflow_versions.c.workflow_id == workflows.c.id) & (workflow_versions.c.version == workflows.c.version))
                    .where(workflows.c.id == workflow_id, workflows.c.version == version, workflows.c.deleted.is_(False))
                ).mappings().first()
                if row is None:
                    raise WorkflowRejected("wf_stale_version")
                name, spec, agents = row["name"], row["spec"], row["agents"]
        validate_spec(spec)
        for snapshot in agents.values():  # the frozen grants and files must still be usable
            self.check_config(snapshot["config"])
        run_id, moment = str(uuid.uuid4()), now()
        with self.engine.begin() as db:
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK})
            if db.execute(select(func.count()).select_from(workflow_runs).where(workflow_runs.c.status == "running")).scalar_one() >= MAX_ACTIVE_WORKFLOWS:
                raise WorkflowRejected("wf_active_limit")
            if db.execute(select(func.count()).select_from(workflow_runs)).scalar_one() >= MAX_STORED_RUNS:
                raise WorkflowRejected("wf_runs_full")
            db.execute(
                insert(workflow_runs).values(
                    id=run_id, workflow_id=workflow_id, version=version, name=name, spec=spec, agents=agents, input=text_input,
                    status="running", created=moment, updated=moment, rerun_of=rerun_of, scheduled_for=scheduled_for,
                )
            )
        return run_id

    def fail_run(self, run_id: str, error: str, *, status: str = "failed", expected_lease: str | None = None, outcome: str = "error") -> None:
        """End a running workflow and stop only the child runs it owns. ``error`` is a message key."""
        query = update(workflow_runs).where(workflow_runs.c.id == run_id, workflow_runs.c.status == "running")
        if expected_lease is not None:
            query = query.where(workflow_runs.c.lease == expected_lease)
        with self.engine.begin() as db:
            changed = db.execute(
                query.values(status=status, outcome_status=outcome, error=error[:300], updated=now(), lease=None, lease_until=None)
            ).rowcount
        if changed or expected_lease is None:
            self.cancel_children(run_id)

    def cancel_children(self, run_id: str) -> None:
        with self.engine.begin() as db:
            db.execute(
                update(runs).where(runs.c.workflow_run_id == run_id, runs.c.status == ACTIVE)
                .values(status="cancelled", error="wf_stopped", updated=now())
            )

    def recover_expired(self) -> None:
        """A workflow never outlives its deadline; expired ones fail (never resubmitted)."""
        limit = now() - timedelta(seconds=self.settings.run_timeout_seconds)
        with self.engine.connect() as db:
            stale = [r[0] for r in db.execute(select(workflow_runs.c.id).where(workflow_runs.c.status == "running", workflow_runs.c.created <= limit))]
        for run_id in stale:
            self.fail_run(run_id, "wf_err_timeout")

    def approve(self, run_id: str, node_id: str, approved: bool) -> bool:
        with self.engine.begin() as db:
            run = db.execute(select(workflow_runs.c.status, workflow_runs.c.created).where(workflow_runs.c.id == run_id)).mappings().first()
            if run is None or run["status"] != "running" or run["created"] + timedelta(seconds=self.settings.run_timeout_seconds) <= now():
                raise WorkflowRejected("wf_not_active")
            changed = db.execute(
                update(workflow_steps)
                .where(workflow_steps.c.workflow_run_id == run_id, workflow_steps.c.node_id == node_id, workflow_steps.c.status == "approval", workflow_steps.c.decision == "pending")
                .values(decision="approved" if approved else "rejected", updated=now())
            ).rowcount
        if changed != 1:
            raise WorkflowRejected("wf_approval_inactive")
        return True

    def orphaned_runs(self, claim_grace_seconds: int = 60) -> list[str]:
        """Running workflows whose controller is gone: the lease expired, or no controller ever took it.

        A live controller (on any replica) heartbeats well inside ``LEASE_SECONDS``, so an expired lease means no
        controller is working on the run. The caller resumes them from their checkpoint (no new workflow).
        """
        moment = now()
        deadline = moment - timedelta(seconds=self.settings.run_timeout_seconds)
        unclaimed = moment - timedelta(seconds=claim_grace_seconds)
        with self.engine.connect() as db:
            rows = db.execute(
                select(workflow_runs.c.id).where(
                    workflow_runs.c.status == "running", workflow_runs.c.created > deadline,
                    (workflow_runs.c.lease_until < moment) | (workflow_runs.c.lease_until.is_(None) & (workflow_runs.c.created < unclaimed)),
                ).order_by(workflow_runs.c.created).limit(10)
            )
            return [r[0] for r in rows]

    def can_continue(self, run_id: str) -> bool:
        """The controller may be relaunched only after its lease expired (no parallel controller)."""
        with self.engine.connect() as db:
            row = db.execute(select(workflow_runs).where(workflow_runs.c.id == run_id)).mappings().first()
        if row is None or row["status"] != "running" or row["created"] + timedelta(seconds=self.settings.run_timeout_seconds) <= now():
            raise WorkflowRejected("wf_not_active")
        if row["lease_until"] and row["lease_until"] > now():
            raise WorkflowRejected("wf_controller_active")
        return True

    # -- schedules -----------------------------------------------------------------------

    def set_schedule(self, workflow_id: str, schedule: Schedule, version: int, text_input: str, *, accepted_schedule: bool, accepted_data_flow: bool) -> datetime:
        if not accepted_schedule:
            raise WorkflowRejected("wf_schedule_consent")
        if not accepted_data_flow:
            raise WorkflowRejected("wf_dataflow_required")
        if not isinstance(text_input, str) or len(text_input) > self.settings.max_prompt_chars:
            raise WorkflowRejected("wf_input_long", limit=self.settings.max_prompt_chars)
        with self.engine.connect() as db:
            row = db.execute(
                select(workflow_versions.c.spec, workflow_versions.c.agents)
                .join(workflows, (workflow_versions.c.workflow_id == workflows.c.id) & (workflow_versions.c.version == workflows.c.version))
                .where(workflows.c.id == workflow_id, workflows.c.version == version, workflows.c.deleted.is_(False))
            ).mappings().first()
        if row is None:
            raise WorkflowRejected("wf_stale_version")
        validate_spec(row["spec"])
        for snapshot in row["agents"].values():  # exactly the checks a manual start makes
            self.check_config(snapshot["config"])
        from zoneinfo import ZoneInfo

        moment = now()
        first = next_occurrence(schedule, moment, ZoneInfo(self.settings.schedule_timezone))
        with self.engine.begin() as db:
            current = db.execute(select(workflows.c.version, workflows.c.deleted).where(workflows.c.id == workflow_id).with_for_update()).mappings().first()
            if current is None or current["deleted"] or current["version"] != version:
                raise WorkflowRejected("wf_stale_version")
            values = {
                "version": version, "schedule": schedule.canonical(), "input": text_input, "consents": {"data_flow": True},
                "enabled": True, "next_run": first, "last_error": "", "updated": moment,
            }
            db.execute(
                pg_insert(workflow_schedules).values(workflow_id=workflow_id, created=moment, **values)
                .on_conflict_do_update(index_elements=[workflow_schedules.c.workflow_id], set_=values)
            )
        return first

    def remove_schedule(self, workflow_id: str) -> None:
        with self.engine.begin() as db:
            db.execute(delete(workflow_schedules).where(workflow_schedules.c.workflow_id == workflow_id))

    def pause_schedule(self, workflow_id: str) -> None:
        with self.engine.begin() as db:
            changed = db.execute(
                update(workflow_schedules).where(workflow_schedules.c.workflow_id == workflow_id)
                .values(enabled=False, next_run=None, last_error="", updated=now())
            ).rowcount
        if not changed:
            raise WorkflowRejected("wf_no_schedule")

    def claim_due(self, moment: datetime) -> list[tuple[dict[str, Any], datetime]]:
        """Claim due occurrences and advance them in ONE transaction (a crash never replays an occurrence)."""
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(self.settings.schedule_timezone)
        launches: list[tuple[dict[str, Any], datetime]] = []
        with self.engine.begin() as db:
            paused = limit_pause_until(db, moment, self.settings.rate_limit_cooldown_seconds)
            rows = db.execute(
                select(workflow_schedules).where(workflow_schedules.c.enabled.is_(True), workflow_schedules.c.next_run <= moment)
                .order_by(workflow_schedules.c.next_run, workflow_schedules.c.workflow_id).limit(20).with_for_update(skip_locked=True)
            ).mappings().all()
            for row in rows:
                due, error, enabled, start = row["next_run"], "", True, None
                current = db.execute(select(workflows.c.version, workflows.c.deleted).where(workflows.c.id == row["workflow_id"])).mappings().first()
                if current is None or current["deleted"]:
                    error, enabled = "wf_sched_deleted", False
                elif current["version"] != row["version"]:
                    error, enabled = "wf_sched_version", False
                elif (moment - due).total_seconds() > MISSED_GRACE_SECONDS:
                    error = "schedule_missed"
                elif paused:
                    error = "schedule_paused"
                elif (
                    db.execute(select(workflow_runs.c.id).where(workflow_runs.c.workflow_id == row["workflow_id"], workflow_runs.c.status == "running").limit(1)).first()
                    or db.execute(select(func.count()).select_from(workflow_runs).where(workflow_runs.c.status == "running")).scalar_one() >= MAX_ACTIVE_WORKFLOWS
                ):
                    continue  # overlap / capacity: wait, at most until the grace period ends
                else:
                    start = {"id": row["workflow_id"], "version": row["version"], "input": row["input"]}
                schedule = Schedule(**row["schedule"])
                db.execute(
                    update(workflow_schedules).where(workflow_schedules.c.workflow_id == row["workflow_id"]).values(
                        next_run=next_occurrence(schedule, moment, zone) if enabled else None, enabled=enabled, last_error=error, updated=now(),
                    )
                )
                if start:
                    launches.append((start, due))
        return launches

    def record_scheduled_start(self, workflow_id: str, run_id: str | None, error: str) -> None:
        with self.engine.begin() as db:
            values: dict[str, Any] = {"last_error": error}
            if run_id:
                values["last_run_id"] = run_id
            db.execute(update(workflow_schedules).where(workflow_schedules.c.workflow_id == workflow_id).values(**values))
