"""Run supervision: one killable worker process per run.

The supervisor (a thread in the API process) starts the worker, watches the run row and
the clock, and kills the whole process tree on cancel, timeout or shutdown. There is no
automatic restart: a crashed worker fails the run (a model call may already have been billed).

What the worker is given (least privilege, see ``worker_environment``): a small allow-list of process
variables, the settings it needs (database, gateway, limits) and none of the API-only secrets (OAuth client
secret, session secret); a private scratch directory per run that is removed afterwards; and, on POSIX,
resource limits (address space, file size, descriptors), no core dumps, umask 077 and no-new-privileges.

This is hardening, not a sandbox: the worker still has the database and gateway credentials it needs and
the container's network. Use the container settings in ``docker-compose.yml`` (read-only root, no
capabilities, memory/pids limits) and an egress policy outside the app for a real boundary.
"""

from __future__ import annotations

import logging
import os
import shutil
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


# Process variables a worker may inherit (everything else in the API's environment is dropped).
_INHERITED = {
    "PATH", "LANG", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "LD_LIBRARY_PATH", "HOME",
    # Windows needs these for Python and DNS to start at all.
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE",
}
_INHERITED_PREFIXES = ("LC_",)
# Settings only the API process needs: the login flow and its session cookie.
_API_ONLY_PREFIXES = ("OAUTH_", "SESSION_", "COOKIE_")


def worker_environment(settings: Settings, base: dict[str, str] | None = None, scratch: Path | None = None) -> dict[str, str]:
    """The environment a worker gets: an allow-list of process variables plus the settings it needs.

    The login secrets never reach a worker (a worker never serves a request), neither does the scheduler switch,
    and tracing/provider/proxy variables are not inherited. ``scratch`` becomes the worker's temp directory.
    """
    source = base if base is not None else os.environ
    env = {
        k: v for k, v in source.items()
        if (k.upper() in _INHERITED or k.upper().startswith(_INHERITED_PREFIXES)) and not k.upper().startswith(_STRIPPED_PREFIXES)
    }
    env.update({k: v for k, v in settings.to_env().items() if not k.startswith(_API_ONLY_PREFIXES)})
    env["AUTH_ENABLED"] = "false"  # the worker has no login; the API process enforces access
    env["SCHEDULER_ENABLED"] = "false"
    env["LANGSMITH_TRACING"] = "false"
    if scratch is not None:
        env["TMPDIR"] = env["TEMP"] = env["TMP"] = str(scratch)
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


def _worker_limits(memory_mb: int, file_mb: int, open_files: int) -> Callable[[], None]:
    """Applied in the child between fork and exec (POSIX only): resource limits and privilege hardening.

    Everything is imported and looked up here, in the parent: the child must not take import or other locks
    that another thread of the API process might hold at the moment of the fork.
    """
    import importlib

    resource: Any = importlib.import_module("resource")  # POSIX only
    prctl: Any = None
    if sys.platform.startswith("linux"):
        try:
            import ctypes

            prctl = ctypes.CDLL(None, use_errno=True).prctl
        except Exception:  # noqa: S110
            prctl = None

    def apply() -> None:
        def cap(which: int, value: int) -> None:
            current_soft, current_hard = resource.getrlimit(which)
            hard = current_hard if current_hard != resource.RLIM_INFINITY and current_hard < value else value
            resource.setrlimit(which, (min(value, hard), hard))

        if memory_mb:
            cap(resource.RLIMIT_AS, memory_mb * 1024 * 1024)
        if file_mb:
            cap(resource.RLIMIT_FSIZE, file_mb * 1024 * 1024)
        if open_files:
            cap(resource.RLIMIT_NOFILE, open_files)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))  # no core dumps (they would contain secrets)
        os.umask(0o077)
        if prctl is not None:
            prctl(38, 1, 0, 0, 0)  # PR_SET_NO_NEW_PRIVS: the worker can never gain privileges through exec

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
            options["preexec_fn"] = _worker_limits(self.settings.worker_memory_mb, self.settings.worker_file_mb, self.settings.worker_open_files)
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        scratch = self.settings.data_dir / "work" / run_id  # private to this run, removed afterwards
        scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            child = subprocess.Popen(  # noqa: S603
                list(command or worker_command(run_id)),
                env=worker_environment(self.settings, scratch=scratch),
                cwd=scratch,  # no stray .env/pyproject from the source tree, nothing shared between runs
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,  # stderr is inherited: class-only log lines
                **options,
            )
        except BaseException:
            shutil.rmtree(scratch, ignore_errors=True)
            raise
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
            shutil.rmtree(scratch, ignore_errors=True)

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
