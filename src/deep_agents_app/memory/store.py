"""Versioned shared text store on Postgres (ported from the previous system's ``memory_tools``).

* Names are lowercase ASCII (1-80 chars), values are inert text (<= 12,000 characters / 32 KB, no control chars).
* Every write needs the exact current revision (0 for a new name); a stale one is a conflict, never an overwrite.
* Deleting is logical: all earlier versions stay in the private history, and restoring writes a NEW revision.
* A (actor, request id) receipt makes a replayed write idempotent; reusing an id with other content is an error.
* Hard limits, no silent purging: 200 names (deleted ones count), 2,000 versions, 4 MiB of history, 5,000 receipts.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, func, insert, select, text, update

from ..db.schema import memory_entries, memory_receipts, memory_versions, runs
from ..runs.store import ACTIVE, now

KEY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
PREFIX = re.compile(r"^[a-z0-9._-]{0,80}$")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
MAX_KEYS, MAX_VERSIONS, MAX_BYTES, MAX_RECEIPTS = 200, 2000, 4 * 1024 * 1024, 5000
MAX_CHARS, MAX_VALUE_BYTES = 12000, 32000
MAX_REVISION = 2_147_483_647
_LOCK = 724_190_553


class MemoryRejected(Exception):
    """A request that breaks a rule. ``key`` is a message key (localized by the caller)."""

    def __init__(self, key: str, **values: object):
        super().__init__(key)
        self.key, self.values = key, values


class MemoryConflict(Exception):
    def __init__(self, revision: int):
        super().__init__("conflict")
        self.revision = revision


def valid_key(key: object) -> str:
    if not isinstance(key, str) or not KEY.match(key):
        raise MemoryRejected("memory_bad_key")
    return key


def _public(row: Any) -> dict[str, Any]:
    return {k: row[k] for k in ("key", "revision", "value", "deleted", "updated", "writer")}


class MemoryStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    # -- reads ---------------------------------------------------------------------------

    def list_entries(self, prefix: str = "", after: str = "", limit: int = 20, include_deleted: bool = False) -> dict[str, Any]:
        """Names and revisions (never values). The prefix is literal, not SQL or a pattern."""
        if not PREFIX.match(prefix) or (after and not KEY.match(after)) or not 1 <= limit <= 50:
            raise MemoryRejected("memory_bad_request")
        query = (
            select(
                memory_entries.c.key, memory_entries.c.revision, memory_entries.c.deleted, memory_entries.c.updated,
                memory_entries.c.writer, func.length(memory_entries.c.value).label("characters"),
            )
            .where(memory_entries.c.key > after, func.substr(memory_entries.c.key, 1, len(prefix)) == prefix)
            .order_by(memory_entries.c.key)
            .limit(limit + 1)
        )
        if not include_deleted:
            query = query.where(memory_entries.c.deleted.is_(False))
        with self.engine.connect() as db:
            rows = [dict(r) for r in db.execute(query).mappings()]
        return {"entries": rows[:limit], "next": rows[limit - 1]["key"] if len(rows) > limit else None}

    def get(self, key: str, revision: int = 0) -> dict[str, Any]:
        valid_key(key)
        if not 0 <= revision <= MAX_REVISION:
            raise MemoryRejected("memory_bad_revision")
        table = memory_entries if revision == 0 else memory_versions
        query = select(table).where(table.c.key == key)
        if revision:
            query = query.where(memory_versions.c.revision == revision)
        with self.engine.connect() as db:
            row = db.execute(query).mappings().first()
        return {"entry": _public(row) if row else None}

    def history(self, key: str, before: int = MAX_REVISION) -> dict[str, Any]:
        valid_key(key)
        if not 1 <= before <= MAX_REVISION:
            raise MemoryRejected("memory_bad_request")
        with self.engine.connect() as db:
            rows = [
                _public(r)
                for r in db.execute(
                    select(memory_versions).where(memory_versions.c.key == key, memory_versions.c.revision < before)
                    .order_by(memory_versions.c.revision.desc()).limit(21)
                ).mappings()
            ]
        return {"versions": rows[:20], "next": rows[19]["revision"] if len(rows) > 20 else None}

    # -- writes --------------------------------------------------------------------------

    def put(self, key: str, value: str, expected_revision: int, actor: str, request_id: str, run_id: str | None = None) -> dict[str, Any]:
        return self._mutate("put", key, value, expected_revision, actor, request_id, run_id)

    def delete(self, key: str, expected_revision: int, actor: str, request_id: str, run_id: str | None = None) -> dict[str, Any]:
        return self._mutate("delete", key, "", expected_revision, actor, request_id, run_id)

    def _mutate(
        self, operation: str, key: str, value: str, expected: int, actor: str, request_id: str, run_id: str | None
    ) -> dict[str, Any]:
        valid_key(key)
        if not isinstance(expected, int) or isinstance(expected, bool) or not 0 <= expected < MAX_REVISION or (operation == "delete" and expected == 0):
            raise MemoryRejected("memory_bad_revision")
        if (
            not isinstance(value, str) or len(value) > MAX_CHARS or len(value.encode("utf-8")) > MAX_VALUE_BYTES or CONTROL.search(value)
        ):
            raise MemoryRejected("memory_bad_value")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
            raise MemoryRejected("memory_bad_request_id")
        digest = hashlib.sha256(
            json.dumps([operation, {"key": key, "value": value, "expectedRevision": expected} if operation == "put" else {"key": key, "expectedRevision": expected}], sort_keys=True, ensure_ascii=True).encode()
        ).hexdigest()
        with self.engine.begin() as db:
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK})  # counts and revisions stay consistent
            if run_id is not None:  # an agent may write only while its run is still active
                status = db.execute(select(runs.c.status).where(runs.c.id == run_id)).scalar_one_or_none()
                if status != ACTIVE:
                    raise MemoryRejected("memory_inactive")
            receipt = db.execute(
                select(memory_receipts.c.digest, memory_receipts.c.result)
                .where(memory_receipts.c.actor == actor, memory_receipts.c.request_id == request_id)
            ).mappings().first()
            if receipt:
                if receipt["digest"] != digest:
                    raise MemoryRejected("memory_request_id_reused")
                return dict(receipt["result"])
            old = db.execute(select(memory_entries.c.revision, memory_entries.c.deleted).where(memory_entries.c.key == key)).mappings().first()
            current = old["revision"] if old else 0
            if current != expected:
                raise MemoryConflict(current)
            if operation == "delete" and (not old or old["deleted"]):
                raise MemoryRejected("memory_already_deleted")
            if not old and db.execute(select(func.count()).select_from(memory_entries)).scalar_one() >= MAX_KEYS:
                raise MemoryRejected("memory_keys_full")
            versions, size = db.execute(
                select(func.count(), func.coalesce(func.sum(func.octet_length(memory_versions.c.value)), 0)).select_from(memory_versions)
            ).one()
            receipts = db.execute(select(func.count()).select_from(memory_receipts)).scalar_one()
            if versions >= MAX_VERSIONS or size + len(value.encode("utf-8")) > MAX_BYTES or receipts >= MAX_RECEIPTS:
                raise MemoryRejected("memory_history_full")
            revision, moment, deleted = current + 1, now(), operation == "delete"
            row = {"key": key, "revision": revision, "value": value, "deleted": deleted, "updated": moment, "writer": actor}
            if old:
                db.execute(update(memory_entries).where(memory_entries.c.key == key).values(**{k: v for k, v in row.items() if k != "key"}))
            else:
                db.execute(insert(memory_entries).values(**row))
            db.execute(insert(memory_versions).values(**row))
            result = {"key": key, "revision": revision, "deleted": deleted, "updated": _iso(moment), "writer": actor}
            db.execute(insert(memory_receipts).values(actor=actor, request_id=request_id, digest=digest, result=result))
        return result


def _iso(moment: datetime) -> str:
    return moment.isoformat()
