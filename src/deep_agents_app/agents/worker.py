"""Worker process entry point: ``python -m deep_agents_app.agents.worker <run-id>``.

One worker executes exactly one run, then exits (0 = the run reached a final state itself,
non-zero = crashed before that). The supervisor in the API process kills it on cancel/timeout.
"""

from __future__ import annotations

import re
import sys

from ..config import get_settings
from ..db.engine import make_engine
from ..runs.store import RunStore
from .runner import execute_run

_RUN_ID = re.compile(r"^[a-f0-9-]{36}$")


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not _RUN_ID.match(argv[1]):
        print("usage: python -m deep_agents_app.agents.worker <run-id>", file=sys.stderr)
        return 2
    settings = get_settings()
    engine = make_engine(settings)
    try:
        execute_run(settings, RunStore(engine, settings), argv[1])
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
