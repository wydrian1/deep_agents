"""Postgres-backed run, event, audit, file and artifact storage."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, func, insert, select, text, update

from ..config import Settings
from ..db.schema import artifacts, input_files, run_events, runs, tool_audit
from .events import serializable

ACTIVE = "running"
FINISHED = ("completed", "failed", "cancelled")
_SLOT_LOCK = 724_190_552
MAX_EVENT_BYTES = 24 * 1024 * 1024
MAX_EVENTS = 20_000
MAX_EVENT_SIZE = 1024 * 1024


class Busy(Exception):
    """All run slots are taken."""


class EventLimit(Exception):
    """The per-run event store is full."""


def now() -> datetime:
    return datetime.now(UTC)


class RunStore:
    def __init__(self, engine: Engine, settings: Settings):
        self.engine, self.settings = engine, settings

    # -- runs ---------------------------------------------------------------------------

    def reserve_run(
        self, *, prompt: str, model: str, mode: str, language: str, tools: list[str], files: list[str]
    ) -> dict[str, Any]:
        """Insert a running run, atomically respecting MAX_CONCURRENT_RUNS across replicas."""
        run_id, created = str(uuid.uuid4()), now()
        with self.engine.begin() as db:
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _SLOT_LOCK})
            self._reap(db)
            active = db.execute(select(func.count()).select_from(runs).where(runs.c.status == ACTIVE)).scalar_one()
            if active >= self.settings.max_concurrent_runs:
                raise Busy
            db.execute(
                insert(runs).values(
                    id=run_id,
                    prompt=prompt,
                    model=model,
                    mode=mode,
                    language=language,
                    status=ACTIVE,
                    tools=tools,
                    files=files,
                    messages=[],
                    created=created,
                    updated=created,
                )
            )
        return self.get(run_id)  # type: ignore[return-value]

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
        self.reap()
        with self.engine.connect() as db:
            rows = db.execute(
                select(
                    runs.c.id, runs.c.prompt, runs.c.model, runs.c.mode, runs.c.status,
                    runs.c.tools, runs.c.steps, runs.c.error, runs.c.created, runs.c.updated,
                )
                .order_by((runs.c.status == ACTIVE).desc(), runs.c.created.desc())
                .limit(limit)
            ).mappings()
            return [dict(row) for row in rows]

    def update(self, run_id: str, **values: Any) -> bool:
        """Update a run only while it is still active (a cancelled run stays cancelled)."""
        with self.engine.begin() as db:
            result = db.execute(
                update(runs).where(runs.c.id == run_id, runs.c.status == ACTIVE).values(updated=now(), **values)
            )
        return result.rowcount == 1

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

    def audit(self, run_id: str, tool: str, outcome: str, duration_ms: int) -> None:
        with self.engine.begin() as db:
            db.execute(
                insert(tool_audit).values(run_id=run_id, tool=tool, outcome=outcome, duration_ms=duration_ms, created=now())
            )

    def tool_calls(self, run_id: str) -> int:
        with self.engine.connect() as db:
            return db.execute(select(func.count()).select_from(tool_audit).where(tool_audit.c.run_id == run_id)).scalar_one()

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
