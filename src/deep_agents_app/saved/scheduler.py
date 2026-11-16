"""Background ticker that starts due saved agents. Every replica may run one; claims use SKIP LOCKED."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from datetime import datetime

from .store import SavedAgentStore

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(
        self, agents: SavedAgentStore, submit: Callable[[str], None], interval_seconds: float,
        extra: Sequence[Callable[[datetime | None], object]] = (),
    ):
        # ``extra`` ticks (workflow schedules) run after the agents' in the same pass.
        self.agents, self.submit, self.interval, self.extra = agents, submit, interval_seconds, list(extra)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self, moment: datetime | None = None) -> list[str]:
        run_ids = self.agents.run_due(moment)
        for run_id in run_ids:
            self.submit(run_id)
        for tick in self.extra:
            try:
                tick(moment)
            except Exception as error:  # one failing source must not starve the others
                log.warning("scheduler extra tick failed: %s", type(error).__name__)
        return run_ids

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception as error:  # a bad tick must not kill the scheduler; no retry storm: next tick only
                log.warning("scheduler tick failed: %s", type(error).__name__)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
