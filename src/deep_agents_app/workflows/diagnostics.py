"""Private, bounded workflow-failure diagnostics (ported from the previous system's workflow_diagnostics).

For an UNEXPECTED controller exception only metadata is stored: run id, time, controller phase, at most four
exceptions of the cause/context chain (class name from an allow-list, otherwise ``ExternalError``), and up to
twenty stack locations per exception (only file and function names of THIS package; every external or dynamic
frame is anonymous). Never the exception message, ``str``/``repr``, source lines, variables, prompts, mail, tokens
or absolute paths. At most 200 reports, one per run, never pruned automatically. Best effort: a failure here can
never replace the workflow's own safe failure/cancellation. The owner can read the reports (they hold metadata
only) through ``GET /api/workflow-diagnostics``; there is deliberately no agent tool.
"""

from __future__ import annotations

import builtins
import re
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, func, insert, select

from ..db.schema import workflow_diagnostics
from ..runs.store import now

MAX_REPORTS, MAX_BYTES = 200, 16384
PHASES = {
    "claim", "checkpoint_open", "compile", "heartbeat", "checkpoint_read", "initial_invoke", "interrupt_check",
    "resume", "continue", "complete", "wait", "cleanup",
}
UUID = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")
PACKAGE = Path(__file__).resolve().parent.parent  # deep_agents_app/


def _allowed_types() -> dict[type, str]:
    types: dict[type, str] = {
        getattr(builtins, name): name
        for name in (
            "ValueError", "TypeError", "RuntimeError", "TimeoutError", "OSError", "PermissionError", "FileNotFoundError", "KeyError",
            "IndexError", "AttributeError", "ImportError", "ModuleNotFoundError", "AssertionError", "RecursionError", "MemoryError",
        )
    }
    try:
        import psycopg

        for name in ("OperationalError", "DatabaseError", "IntegrityError", "ProgrammingError", "InterfaceError"):
            types[getattr(psycopg, name)] = "psycopg." + name
    except Exception:  # noqa: S110
        pass
    try:
        from sqlalchemy import exc as sa_exc

        for name in ("OperationalError", "DatabaseError", "IntegrityError", "ProgrammingError", "InterfaceError"):
            types[getattr(sa_exc, name)] = "sqlalchemy." + name
    except Exception:  # noqa: S110
        pass
    try:
        from langgraph import errors as graph_errors

        for name in ("GraphRecursionError", "InvalidUpdateError", "EmptyInputError", "TaskNotFound"):
            types[getattr(graph_errors, name)] = "langgraph." + name
    except Exception:  # noqa: S110
        pass
    return types


ERROR_TYPES = _allowed_types()


def exception_metadata(error: BaseException | None) -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    seen: set[int] = set()
    while error is not None and id(error) not in seen and len(chain) < 4:
        seen.add(id(error))
        frames: list[dict[str, Any]] = []
        tb = error.__traceback__
        while tb is not None:
            code = tb.tb_frame.f_code
            try:
                relative: Path | None = Path(code.co_filename).resolve().relative_to(PACKAGE)
            except (ValueError, OSError):
                relative = None
            owned = relative is not None and relative.suffix == ".py" and len(relative.parts) <= 3
            frames.append(
                {
                    "file": "/".join(relative.parts) if owned and relative else "[external]",
                    "function": code.co_name if owned and IDENTIFIER.match(code.co_name) else "[omitted]",
                    "line": tb.tb_lineno,
                }
            )
            tb = tb.tb_next
        item: dict[str, Any] = {"type": ERROR_TYPES.get(type(error), "ExternalError"), "frames": frames[-20:]}
        sqlstate = getattr(error, "sqlstate", None) or getattr(getattr(error, "orig", None), "sqlstate", None)
        if type(error) in ERROR_TYPES and isinstance(sqlstate, str) and re.fullmatch(r"[0-9A-Z]{5}", sqlstate):
            item["sqlstate"] = sqlstate  # the SQLSTATE class helps separate a lock timeout from a logic error
        chain.append(item)
        error = error.__cause__ if error.__cause__ is not None else (None if error.__suppress_context__ else error.__context__)
    return chain


def list_reports(engine: Engine, limit: int = MAX_REPORTS) -> list[dict[str, Any]]:
    """Newest first. The bodies are metadata only (see the module docstring)."""
    with engine.connect() as db:
        rows = db.execute(select(workflow_diagnostics).order_by(workflow_diagnostics.c.created.desc()).limit(min(limit, MAX_REPORTS))).mappings()
        return [{"run_id": r["run_id"], "phase": r["phase"], "created": r["created"].isoformat(), "body": r["body"]} for r in rows]


def record_failure(engine: Engine, run_id: str, phase: str, error: BaseException) -> bool:
    """One report per run; no retries, no pruning, no stdout, never raises."""
    try:
        if not isinstance(run_id, str) or not UUID.match(run_id) or phase not in PHASES:
            return False
        body = {"version": 1, "exceptions": exception_metadata(error)}
        if len(str(body)) > MAX_BYTES:
            return False
        with engine.begin() as db:
            if db.execute(select(workflow_diagnostics.c.id).where(workflow_diagnostics.c.run_id == run_id).limit(1)).first():
                return True  # a report for this run is kept, not overwritten
            if db.execute(select(func.count()).select_from(workflow_diagnostics)).scalar_one() >= MAX_REPORTS:
                return False
            db.execute(insert(workflow_diagnostics).values(run_id=run_id, phase=phase, created=now(), body=body))
        return True
    except Exception:
        return False
