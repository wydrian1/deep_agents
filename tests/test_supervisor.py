import subprocess
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from deep_agents_app.agents import supervisor as supervisor_module
from deep_agents_app.agents import worker
from deep_agents_app.agents.supervisor import Supervisor, worker_environment
from deep_agents_app.config import Settings
from deep_agents_app.main import create_app

from .conftest import make_settings, wait_for_status


def reserve(store):
    return store.reserve_run(prompt="p", model="m", mode="test", language="en", tools=[], files=[])["id"]


def sleeper(seconds=60):
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


@pytest.fixture
def children(monkeypatch):
    """Capture started worker processes so tests can assert they are really dead."""
    started = []
    real = subprocess.Popen

    def spy(*args, **kwargs):
        process = real(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(supervisor_module.subprocess, "Popen", spy)
    yield started
    for process in started:
        if process.poll() is None:
            process.kill()


def test_settings_round_trip_through_the_environment(settings):
    rebuilt = Settings(_env_file=None, **{k.lower(): v for k, v in settings.to_env().items()})  # type: ignore[arg-type]
    assert rebuilt == settings
    assert settings.to_env()["LLM_API_KEY"] == "test-key"


def test_worker_environment_is_an_allow_list_and_keeps_the_settings_a_worker_needs(settings):
    base = {"PATH": "/bin", "LC_ALL": "C", "LANGSMITH_API_KEY": "x", "langchain_tracing_v2": "true", "HTTPS_PROXY": "http://p", "OTHER": "1", "AWS_SECRET_ACCESS_KEY": "s"}
    env = worker_environment(settings, base)
    assert env["PATH"] == "/bin" and env["LC_ALL"] == "C"
    assert not {"OTHER", "AWS_SECRET_ACCESS_KEY", "LANGSMITH_API_KEY", "langchain_tracing_v2", "HTTPS_PROXY"} & set(env)
    assert env["LANGSMITH_TRACING"] == "false" and env["DATABASE_SCHEMA"] == settings.database_schema
    assert env["LLM_API_KEY"] == "test-key" and env["DATABASE_URL"] == settings.database_url  # what a run really needs


def test_worker_environment_never_carries_the_login_secrets(settings):
    secret = settings.model_copy(update={
        "auth_enabled": True, "oauth_client_secret": "client-secret-value", "session_secret": "s" * 40, "oauth_client_id": "id",
    })
    env = worker_environment(secret, {"PATH": "/bin"})
    assert env["AUTH_ENABLED"] == "false" and env["SCHEDULER_ENABLED"] == "false"
    assert not [k for k in env if k.startswith(("OAUTH_", "SESSION_", "COOKIE_"))]
    assert "client-secret-value" not in env.values() and "s" * 40 not in env.values()
    rebuilt = Settings(_env_file=None, **{k.lower(): v for k, v in env.items() if k.lower() in Settings.model_fields})  # type: ignore[arg-type]
    assert rebuilt.auth_enabled is False  # a worker's settings are valid without the login configuration


def test_worker_environment_points_temp_files_at_the_private_scratch_directory(settings, tmp_path):
    env = worker_environment(settings, {"PATH": "/bin", "TMPDIR": "/tmp"}, scratch=tmp_path)
    assert env["TMPDIR"] == env["TEMP"] == env["TMP"] == str(tmp_path)


def test_a_worker_runs_in_a_private_scratch_directory_that_is_removed(settings, store, children):
    run_id = reserve(store)
    seen = settings.data_dir / "work" / run_id
    script = "import os,sys; open('probe.txt','w').write(os.getcwd()); sys.exit(1)"
    Supervisor(settings, store, poll_seconds=0.05).supervise(run_id, [sys.executable, "-c", script])
    assert not seen.exists()  # nothing a run wrote there outlives it
    assert (settings.data_dir / "work").is_dir()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX resource limits")
def test_posix_workers_get_resource_limits_no_core_dumps_and_a_private_umask(settings, store, children):
    run_id = reserve(store)
    limited = settings.model_copy(update={"worker_file_mb": 7, "worker_open_files": 300})
    out = settings.data_dir / "limits.txt"
    script = (
        "import resource,os,sys;"
        f"open({str(out)!r},'w').write(repr((resource.getrlimit(resource.RLIMIT_FSIZE)[0],resource.getrlimit(resource.RLIMIT_NOFILE)[0],"
        "resource.getrlimit(resource.RLIMIT_CORE)[0],os.umask(0o022))))"
    )
    Supervisor(limited, store, poll_seconds=0.05).supervise(run_id, [sys.executable, "-c", script])
    assert out.read_text() == repr((7 * 1024 * 1024, 300, 0, 0o077))


def test_worker_rejects_bad_arguments(capsys):
    assert worker.main(["worker"]) == 2
    assert worker.main(["worker", "../../etc/passwd"]) == 2
    assert "usage" in capsys.readouterr().err


def test_a_crashing_worker_fails_the_run(settings, store, children):
    run_id = reserve(store)
    Supervisor(settings, store, poll_seconds=0.05).supervise(run_id, [sys.executable, "-c", "raise SystemExit(3)"])
    assert (store.get(run_id)["status"], store.get(run_id)["error"]) == ("failed", "run_failed")


def test_a_worker_that_exits_without_finishing_fails_the_run(settings, store, children):
    run_id = reserve(store)
    Supervisor(settings, store, poll_seconds=0.05).supervise(run_id, [sys.executable, "-c", "pass"])
    assert store.get(run_id)["status"] == "failed"


def test_a_worker_that_finished_the_run_is_left_alone(settings, store, children):
    run_id = reserve(store)
    store.update(run_id, status="completed", result="done")  # what execute_run does
    Supervisor(settings, store, poll_seconds=0.05).supervise(run_id, sleeper())  # not even started
    assert store.get(run_id)["status"] == "completed" and not children


def test_timeout_kills_the_worker_hard(settings, store, children):
    run_id = reserve(store)
    short = Supervisor(settings.model_copy(update={"run_timeout_seconds": 1}), store, poll_seconds=0.05)
    started = time.monotonic()
    short.supervise(run_id, sleeper())
    assert time.monotonic() - started < 15
    assert children[0].poll() is not None
    assert (store.get(run_id)["status"], store.get(run_id)["error"]) == ("failed", "run_timeout")


def test_cancel_kills_the_worker_and_stays_cancelled(settings, store, children):
    run_id = reserve(store)
    thread = threading.Thread(target=Supervisor(settings, store, poll_seconds=0.05).supervise, args=(run_id, sleeper()))
    thread.start()
    deadline = time.monotonic() + 10
    while not children and time.monotonic() < deadline:
        time.sleep(0.05)
    assert children, "worker never started"
    assert store.cancel(run_id)
    thread.join(timeout=15)
    assert not thread.is_alive() and children[0].poll() is not None
    assert (store.get(run_id)["status"], store.get(run_id)["error"]) == ("cancelled", "run_cancelled")


def test_shutdown_kills_running_workers_and_fails_their_runs(settings, store, children):
    run_id = reserve(store)
    supervisor = Supervisor(settings, store, poll_seconds=0.05)
    thread = threading.Thread(target=supervisor.supervise, args=(run_id, sleeper()))
    thread.start()
    deadline = time.monotonic() + 10
    while not children and time.monotonic() < deadline:
        time.sleep(0.05)
    assert children, "worker never started"
    supervisor.shutdown()
    thread.join(timeout=15)
    assert not thread.is_alive() and children[0].poll() is not None
    assert (store.get(run_id)["status"], store.get(run_id)["error"]) == ("failed", "run_orphaned")


def test_a_real_worker_process_completes_a_run(database_url, tmp_path):
    """End to end: API -> supervisor -> `python -m ...worker` subprocess -> Postgres."""
    settings = make_settings(database_url, "t_proc_x4", tmp_path, run_mode="process")
    try:
        with TestClient(create_app(settings)) as client:
            run_id = client.post("/api/runs", json={"prompt": "hello", "mode": "test"}).json()["id"]
            run = wait_for_status(client, run_id, seconds=90)
            state = client.get(f"/api/langgraph/threads/{run_id}/state").json()
        assert run["status"] == "completed" and "Test report" in run["result"]
        assert state["metadata"]["stream_cursor"] > 0
    finally:
        from sqlalchemy import create_engine, text

        engine = create_engine(settings.sqlalchemy_url)
        with engine.begin() as db:
            db.execute(text('DROP SCHEMA IF EXISTS "t_proc_x4" CASCADE'))
        engine.dispose()
