"""Postgres-backed run, event, audit, file and artifact storage."""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, func, insert, select, text, update

from ..config import Settings
from ..db.schema import artifacts, input_files, llm_calls, run_events, runs, tool_audit
from .events import serializable

ACTIVE = "running"
FINISHED = ("completed", "failed", "cancelled")
_SLOT_LOCK = 724_190_552
MAX_EVENT_BYTES = 24 * 1024 * 1024
MAX_EVENTS = 20_000
MAX_EVENT_SIZE = 1024 * 1024
MAX_DETAIL_ROWS = 200


class Busy(Exception):
    """All run slots are taken."""


class EventLimit(Exception):
    """The per-run event store is full."""


class DiskLow(Exception):
    """Not enough free space under DATA_DIR to start a run safely."""


def now() -> datetime:
    return datetime.now(UTC)


class RunStore:
    def __init__(self, engine: Engine, settings: Settings):
        self.engine, self.settings = engine, settings

    # -- runs ---------------------------------------------------------------------------

    def reserve_in(
        self, db: Any, *, prompt: str, model: str, mode: str, language: str, tools: list[str], files: list[str],
        agent_id: str | None = None, scheduled_for: datetime | None = None, rerun_of: str | None = None,
    ) -> str:
        """Insert a running run inside the caller's transaction, respecting MAX_CONCURRENT_RUNS across replicas.

        The advisory lock lives until that transaction ends, so slot counting stays atomic.
        """
        run_id, created = str(uuid.uuid4()), now()
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.settings.data_dir).free < self.settings.min_free_disk_mb * 1024 * 1024:
            raise DiskLow
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _SLOT_LOCK})
        self._reap(db)
        active = db.execute(select(func.count()).select_from(runs).where(runs.c.status == ACTIVE)).scalar_one()
        if active >= self.settings.max_concurrent_runs:
            raise Busy
        db.execute(
            insert(runs).values(
                id=run_id, prompt=prompt, model=model, mode=mode, language=language, status=ACTIVE,
                tools=tools, files=files, messages=[], agent_id=agent_id, scheduled_for=scheduled_for,
                rerun_of=rerun_of, created=created, updated=created,
            )
        )
        return run_id

    def reserve_run(
        self, *, prompt: str, model: str, mode: str, language: str, tools: list[str], files: list[str],
        agent_id: str | None = None, rerun_of: str | None = None,
    ) -> dict[str, Any]:
        """Insert a running run in its own transaction."""
        with self.engine.begin() as db:
            run_id = self.reserve_in(
                db, prompt=prompt, model=model, mode=mode, language=language, tools=tools, files=files, agent_id=agent_id,
                rerun_of=rerun_of,
            )
        return self.get(run_id)  # type: ignore[return-value]

    def has_active_run(self, db: Any, agent_id: str) -> bool:
        return db.execute(select(runs.c.id).where(runs.c.agent_id == agent_id, runs.c.status == ACTIVE).limit(1)).first() is not None

    def _reap(self, db: Any) -> None:
        """A worker never outlives the timeout; recover orphaned reservations (no resubmission)."""
        limit = now() - timedelta(seconds=self.settings.run_timeout_seconds + 60)
        db.execute(
            update(runs)
            .where(runs.c.status == ACTIVE, runs.c.created < limit)
            .values(status="failed", error="run_orphaned", updated=now())
        )

    def reap(self) -> None:
        with self.engine.begin() as db:
            self._reap(db)

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as db:
            row = db.execute(select(runs).where(runs.c.id == run_id)).mappings().first()
        return dict(row) if row else None

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        """The active and most recent runs, each with its metrics summary, tool-call count and outputs."""
        self.reap()
        with self.engine.connect() as db:
            rows = [
                dict(row)
                for row in db.execute(
                    select(
                        runs.c.id, runs.c.prompt, runs.c.model, runs.c.mode, runs.c.status, runs.c.language,
                        runs.c.tools, runs.c.steps, runs.c.error, runs.c.outcome_status, runs.c.agent_id,
                        runs.c.scheduled_for, runs.c.rerun_of, runs.c.created, runs.c.updated,
                    )
                    .order_by((runs.c.status == ACTIVE).desc(), runs.c.created.desc())
                    .limit(limit)
                ).mappings()
            ]
            ids = [r["id"] for r in rows]
            outputs: dict[str, list[dict[str, Any]]] = {i: [] for i in ids}
            if ids:
                for a in db.execute(
                    select(artifacts.c.run_id, artifacts.c.id, artifacts.c.name, artifacts.c.title, artifacts.c.size)
                    .where(artifacts.c.run_id.in_(ids)).order_by(artifacts.c.created)
                ).mappings():
                    outputs[a["run_id"]].append({k: a[k] for k in ("id", "name", "title", "size")})
        summaries = self.summarize(rows)
        for r in rows:
            r["metrics"] = summaries.get(r["id"])
            r["tool_calls"] = summaries[r["id"]]["tool_calls"] if r["id"] in summaries else 0
            r["artifacts"] = outputs[r["id"]]
        return rows

    def update(self, run_id: str, **values: Any) -> bool:
        """Update a run only while it is still active (a cancelled run stays cancelled)."""
        with self.engine.begin() as db:
            result = db.execute(
                update(runs).where(runs.c.id == run_id, runs.c.status == ACTIVE).values(updated=now(), **values)
            )
        return result.rowcount == 1

    def active_count(self) -> int:
        with self.engine.connect() as db:
            return db.execute(select(func.count()).select_from(runs).where(runs.c.status == ACTIVE)).scalar_one()

    def is_active(self, run_id: str) -> bool:
        with self.engine.connect() as db:
            status = db.execute(select(runs.c.status).where(runs.c.id == run_id)).scalar_one_or_none()
        return status == ACTIVE

    def cancel(self, run_id: str) -> bool:
        return self.update(run_id, status="cancelled", error="run_cancelled")

    # -- events -------------------------------------------------------------------------

    def append_event(self, run_id: str, event: str, data: Any) -> int:
        payload = serializable(data)
        encoded = json.dumps(payload, ensure_ascii=True)
        if len(encoded) > MAX_EVENT_SIZE:
            raise EventLimit("event too large")
        with self.engine.begin() as db:
            row = db.execute(
                update(runs)
                .where(runs.c.id == run_id, runs.c.event_count < MAX_EVENTS, runs.c.event_bytes + len(encoded) <= MAX_EVENT_BYTES)
                .values(event_count=runs.c.event_count + 1, event_bytes=runs.c.event_bytes + len(encoded))
                .returning(runs.c.id)
            ).first()
            if row is None:
                raise EventLimit("event store full")
            event_id = db.execute(
                insert(run_events).values(run_id=run_id, event=event, data=payload, created=now()).returning(run_events.c.id)
            ).scalar_one()
            if event == "values" and isinstance(payload, dict):
                # State projection: conversation messages only (no virtual files or internals).
                db.execute(update(runs).where(runs.c.id == run_id).values(messages=payload.get("messages", [])))
        return event_id

    def events_after(self, run_id: str, cursor: int, limit: int = 64) -> list[dict[str, Any]]:
        with self.engine.connect() as db:
            rows = db.execute(
                select(run_events.c.id, run_events.c.event, run_events.c.data)
                .where(run_events.c.run_id == run_id, run_events.c.id > cursor)
                .order_by(run_events.c.id)
                .limit(limit)
            ).mappings()
            return [dict(row) for row in rows]

    def last_event_id(self, run_id: str) -> int:
        with self.engine.connect() as db:
            return db.execute(
                select(func.coalesce(func.max(run_events.c.id), 0)).where(run_events.c.run_id == run_id)
            ).scalar_one()

    # -- tool audit (operation, outcome and duration only; never arguments or content) ---

    def audit_start(self, run_id: str, tool: str, operation: str) -> int:
        """Record the call BEFORE it runs: an interrupted call stays ``started`` and never counts as success."""
        with self.engine.begin() as db:
            return db.execute(
                insert(tool_audit)
                .values(run_id=run_id, tool=tool, operation=operation, outcome="started", duration_ms=0, created=now())
                .returning(tool_audit.c.id)
            ).scalar_one()

    def audit_finish(self, audit_id: int, outcome: str, duration_ms: int) -> None:
        with self.engine.begin() as db:
            db.execute(update(tool_audit).where(tool_audit.c.id == audit_id).values(outcome=outcome, duration_ms=duration_ms))

    def tool_calls(self, run_id: str, tools: set[str] | None = None) -> int:
        query = select(func.count()).select_from(tool_audit).where(tool_audit.c.run_id == run_id)
        if tools is not None:
            query = query.where(tool_audit.c.tool.in_(tools))
        with self.engine.connect() as db:
            return db.execute(query).scalar_one()

    def audit_errors(self, run_id: str) -> int:
        """Failed or interrupted (never finished) tool calls of a run."""
        with self.engine.connect() as db:
            return db.execute(
                select(func.count()).select_from(tool_audit)
                .where(tool_audit.c.run_id == run_id, tool_audit.c.outcome.in_(("error", "started")))
            ).scalar_one()

    # -- model-call metrics (content-free) ------------------------------------------------

    def record_llm_call(self, **values: Any) -> None:
        with self.engine.begin() as db:
            db.execute(insert(llm_calls).values(**values))

    def summarize(self, rows: list[dict[str, Any]], moment: datetime | None = None) -> dict[str, dict[str, Any]]:
        """Per-run timing/token summary (ported from the previous system's ``summarize``; snake_case keys).

        ``wall_ms`` is the run's wall time; ``startup_ms`` the wait before the first model call; ``model_ms`` and
        ``tool_ms`` sum the measured calls; ``other_ms`` is the remainder. Token fields are ``None`` (not 0) when
        the gateway reported no usage.
        """
        if not rows:
            return {}
        moment = moment or now()
        ids = [r["id"] for r in rows]
        with self.engine.connect() as db:
            calls = {
                c["run_id"]: c
                for c in db.execute(
                    select(
                        llm_calls.c.run_id,
                        func.count().label("calls"),
                        func.count().filter(llm_calls.c.scope == "subagent").label("sub"),
                        func.count().filter(llm_calls.c.status != "ok").label("errors"),
                        func.sum(llm_calls.c.duration_ms).label("model_ms"),
                        func.min(llm_calls.c.started).label("first_start"),
                        func.sum(llm_calls.c.input_tokens).label("i"),
                        func.sum(llm_calls.c.output_tokens).label("o"),
                        func.sum(llm_calls.c.cache_read_tokens).label("c"),
                        func.sum(llm_calls.c.reasoning_tokens).label("r"),
                        func.count(llm_calls.c.input_tokens).label("with_usage"),
                    ).where(llm_calls.c.run_id.in_(ids)).group_by(llm_calls.c.run_id)
                ).mappings()
            }
            tools = {
                t["run_id"]: t
                for t in db.execute(
                    select(
                        tool_audit.c.run_id, func.count().label("calls"),
                        func.coalesce(func.sum(tool_audit.c.duration_ms), 0).label("ms"),
                    ).where(tool_audit.c.run_id.in_(ids)).group_by(tool_audit.c.run_id)
                ).mappings()
            }

        def total(value: Any) -> int | None:
            return int(value) if value is not None else None

        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            call, tool = calls.get(row["id"]), tools.get(row["id"])
            end = moment if row["status"] == ACTIVE else row["updated"]
            wall = max(0, round((end - row["created"]).total_seconds() * 1000))
            measured = call is not None
            model_ms = total(call["model_ms"]) if call else None
            tool_ms = total(tool["ms"]) if tool else 0
            startup = max(0, round((call["first_start"] - row["created"]).total_seconds() * 1000)) if call else None
            other = max(0, wall - (startup or 0) - (model_ms or 0) - (tool_ms or 0)) if measured else None
            has_usage = bool(call and call["with_usage"])
            result[row["id"]] = {
                "wall_ms": wall, "startup_ms": startup, "model_ms": model_ms, "tool_ms": tool_ms, "other_ms": other,
                "model_calls": int(call["calls"]) if call else 0, "subagent_calls": int(call["sub"]) if call else 0,
                "error_calls": int(call["errors"]) if call else 0, "tool_calls": int(tool["calls"]) if tool else 0,
                "input_tokens": total(call["i"]) if has_usage and call else None,
                "output_tokens": total(call["o"]) if has_usage and call else None,
                "cache_read_tokens": total(call["c"]) if has_usage and call and call["c"] is not None else None,
                "reasoning_tokens": total(call["r"]) if has_usage and call and call["r"] is not None else None,
                "usage_calls": int(call["with_usage"]) if call else 0, "measured": measured,
            }
        return result

    def run_metrics(self, run_id: str) -> dict[str, Any] | None:
        """Summary plus the per-call and per-tool detail (at most 200 of each; ``truncated`` says if cut)."""
        run = self.get(run_id)
        if run is None:
            return None
        created = run["created"]
        with self.engine.connect() as db:
            call_rows = [
                dict(r)
                for r in db.execute(
                    select(llm_calls).where(llm_calls.c.run_id == run_id).order_by(llm_calls.c.id).limit(MAX_DETAIL_ROWS + 1)
                ).mappings()
            ]
            tool_rows = [
                dict(r)
                for r in db.execute(
                    select(tool_audit).where(tool_audit.c.run_id == run_id).order_by(tool_audit.c.id).limit(MAX_DETAIL_ROWS + 1)
                ).mappings()
            ]
        calls = [
            {
                "seq": i + 1, "offset_ms": max(0, round((c["started"] - created).total_seconds() * 1000)),
                "duration_ms": c["duration_ms"], "scope": c["scope"], "node": c["node"], "status": c["status"],
                "error": c["error"], "http_status": c["http_status"], "input_tokens": c["input_tokens"],
                "output_tokens": c["output_tokens"], "cache_read_tokens": c["cache_read_tokens"],
                "reasoning_tokens": c["reasoning_tokens"], "model": c["model"],
            }
            for i, c in enumerate(call_rows[:MAX_DETAIL_ROWS])
        ]
        tools = [
            {
                "tool": t["tool"], "operation": t["operation"], "outcome": t["outcome"],
                "offset_ms": max(0, round((t["created"] - created).total_seconds() * 1000)), "duration_ms": t["duration_ms"],
            }
            for t in tool_rows[:MAX_DETAIL_ROWS]
        ]
        return {
            "summary": self.summarize([run])[run_id], "calls": calls, "tools": tools,
            "truncated": len(call_rows) > MAX_DETAIL_ROWS or len(tool_rows) > MAX_DETAIL_ROWS,
        }

    def metrics_summary(self, days: int) -> list[dict[str, Any]]:
        """Per-model aggregates over the last ``days`` days (not in the previous system; cheap and useful)."""
        since = now() - timedelta(days=days)
        with self.engine.connect() as db:
            rows = db.execute(
                select(
                    llm_calls.c.model,
                    func.count().label("calls"),
                    func.count().filter(llm_calls.c.status == "error").label("errors"),
                    func.sum(llm_calls.c.input_tokens).label("input_tokens"),
                    func.sum(llm_calls.c.output_tokens).label("output_tokens"),
                    func.sum(llm_calls.c.cache_read_tokens).label("cache_read_tokens"),
                    func.sum(llm_calls.c.reasoning_tokens).label("reasoning_tokens"),
                    func.avg(llm_calls.c.duration_ms).label("avg_duration_ms"),
                    func.max(llm_calls.c.duration_ms).label("max_duration_ms"),
                )
                .where(llm_calls.c.started >= since)
                .group_by(llm_calls.c.model)
                .order_by(func.count().desc())
            ).mappings()
            keys = ("input_tokens", "output_tokens", "cache_read_tokens", "reasoning_tokens", "avg_duration_ms")
            return [{**r} | {k: (int(r[k]) if r[k] is not None else None) for k in keys} for r in rows]

    # -- input files ----------------------------------------------------------------------

    def add_input_file(self, file_id: str, name: str, size: int, sha256: str) -> dict[str, Any]:
        with self.engine.begin() as db:
            db.execute(insert(input_files).values(id=file_id, name=name, size=size, sha256=sha256, created=now()))
        return {"id": file_id, "name": name, "size": size, "sha256": sha256}

    def get_input_files(self, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        with self.engine.connect() as db:
            rows = db.execute(select(input_files).where(input_files.c.id.in_(ids))).mappings()
            return [dict(row) for row in rows]

    # -- artifacts ------------------------------------------------------------------------

    def artifact_usage(self, run_id: str) -> tuple[int, int]:
        with self.engine.connect() as db:
            count, size = db.execute(
                select(func.count(), func.coalesce(func.sum(artifacts.c.size), 0)).where(artifacts.c.run_id == run_id)
            ).one()
        return int(count), int(size)

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as db:
            row = db.execute(select(artifacts).where(artifacts.c.id == artifact_id)).mappings().first()
        return dict(row) if row else None

    def list_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as db:
            rows = db.execute(
                select(artifacts.c.id, artifacts.c.name, artifacts.c.title, artifacts.c.size)
                .where(artifacts.c.run_id == run_id)
                .order_by(artifacts.c.created)
            ).mappings()
            return [dict(row) for row in rows]

    def add_artifact(self, artifact_id: str, run_id: str, name: str, title: str, size: int, sha256: str) -> None:
        with self.engine.begin() as db:
            db.execute(
                insert(artifacts).values(
                    id=artifact_id, run_id=run_id, name=name, title=title, size=size, sha256=sha256, created=now()
                )
            )
