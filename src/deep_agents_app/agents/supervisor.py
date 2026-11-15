"""Run supervision: one killable worker process per run.

The supervisor (a thread in the API process) starts the worker, watches the run row and
the clock, and kills the whole process tree on cancel, timeout or shutdown. There is no
automatic restart: a crashed worker fails the run (a model call may already have been billed).

This provides crash isolation and hard stops. It is NOT a security boundary: the worker has
the same secrets and network access as the API process.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess  # noqa: S404
import sys
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import Settings
from ..runs.store import RunStore
from .runner import ENV_PREFIXES_REMOVED, execute_run

log = logging.getLogger(__name__)
POLL_SECONDS = 0.5
_STRIPPED_PREFIXES = ENV_PREFIXES_REMOVED


def worker_command(run_id: str) -> list[str]:
    return [sys.executable, "-I", "-m", "deep_agents_app.agents.worker", run_id]


def worker_environment(settings: Settings, base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a worker gets: the parent's, minus tracing/proxy overrides, plus the exact settings."""
    env = {k: v for k, v in (base if base is not None else os.environ).items() if not k.upper().startswith(_STRIPPED_PREFIXES)}
    env.update(settings.to_env())
    env["LANGSMITH_TRACING"] = "false"
    return env


def terminate_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
            subprocess.run(  # noqa: S603
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)  # the worker leads its own process group
    except (ProcessLookupError, PermissionError, subprocess.SubprocessError):
        process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def _limit_memory(megabytes: int) -> Callable[[], None]:
    def apply() -> None:  # runs in the child between fork and exec
        import resource

        limit = megabytes * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))  # type: ignore[attr-defined,unused-ignore]

    return apply


class Supervisor:
    """Runs workers on a bounded pool of supervisor threads (one per concurrent run)."""

    def __init__(self, settings: Settings, store: RunStore, poll_seconds: float = POLL_SECONDS):
        self.settings, self.store, self.poll_seconds = settings, store, poll_seconds
        self.pool = ThreadPoolExecutor(max_workers=settings.max_concurrent_runs, thread_name_prefix="supervisor")
        self._children: dict[str, subprocess.Popen[bytes]] = {}
        self._lock = threading.Lock()

    def submit(self, run_id: str) -> None:
        self.pool.submit(self._guarded, run_id)

    def _guarded(self, run_id: str) -> None:
        try:
            self.supervise(run_id)
        except Exception as error:  # never leave a run stuck in "running"
            log.error("supervisor for run %s crashed: %s", run_id, type(error).__name__)
            self.store.update(run_id, status="failed", error="run_failed")

    def supervise(self, run_id: str, command: Sequence[str] | None = None) -> None:
        run = self.store.get(run_id)
        if not run or run["status"] != "running":
            return
        created: datetime = run["created"]
        options: dict[str, Any] = {}
        if sys.platform == "win32":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
            if self.settings.worker_memory_mb:
                options["preexec_fn"] = _limit_memory(self.settings.worker_memory_mb)
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        child = subprocess.Popen(  # noqa: S603
            list(command or worker_command(run_id)),
            env=worker_environment(self.settings),
            cwd=self.settings.data_dir,  # no stray .env/pyproject from the source tree
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,  # stderr is inherited: class-only log lines
            **options,
        )
        with self._lock:
            self._children[run_id] = child
        try:
            while child.poll() is None:
                if not self.store.is_active(run_id):
                    terminate_tree(child)  # cancelled (status is already final)
                    return
                if (datetime.now(UTC) - created).total_seconds() >= self.settings.run_timeout_seconds:
                    terminate_tree(child)
                    self.store.update(run_id, status="failed", error="run_timeout")
                    return
                time.sleep(self.poll_seconds)
            if child.returncode or self.store.is_active(run_id):
                # A worker that crashed, or exited without reaching a final state, failed the run.
                # No-op if the worker already recorded a specific cause.
                self.store.update(run_id, status="failed", error="run_failed")
        finally:
            with self._lock:
                self._children.pop(run_id, None)
            terminate_tree(child)  # also reaps stray grandchildren of a finished worker

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
        with self._lock:
            children = dict(self._children)
        for run_id, child in children.items():
            terminate_tree(child)
            self.store.update(run_id, status="failed", error="run_orphaned")


class ThreadExecutor:
    """In-process execution for tests/debugging (RUN_MODE=thread). Hung runs cannot be killed."""

    def __init__(self, settings: Settings, store: RunStore):
        self.settings, self.store = settings, store
        self.pool = ThreadPoolExecutor(max_workers=settings.max_concurrent_runs, thread_name_prefix="run")

    def submit(self, run_id: str) -> None:
        self.pool.submit(self._guarded, run_id)

    def _guarded(self, run_id: str) -> None:
        try:
            execute_run(self.settings, self.store, run_id)
        except Exception as error:
            log.error("run %s crashed: %s", run_id, type(error).__name__)
            self.store.update(run_id, status="failed", error="run_failed")

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)


def make_executor(settings: Settings, store: RunStore) -> Supervisor | ThreadExecutor:
    return Supervisor(settings, store) if settings.run_mode == "process" else ThreadExecutor(settings, store)
