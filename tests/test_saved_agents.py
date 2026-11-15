import threading
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from deep_agents_app.db.engine import make_engine
from deep_agents_app.db.schema import runs, saved_agents
from deep_agents_app.runs.store import Busy, RunStore, now
from deep_agents_app.saved.schedule import Schedule
from deep_agents_app.saved.scheduler import Scheduler
from deep_agents_app.saved.store import (
    MAX_AGENTS,
    MISSED_GRACE_SECONDS,
    AgentLimit,
    AgentNotFound,
    RevisionConflict,
    SavedAgentStore,
)

CONFIG = {"prompt": "report", "model": None, "mode": "test", "language": "en", "tools": [], "files": []}
DAILY = Schedule(kind="daily", hour=8, minute=0)


@pytest.fixture
def agents(settings, store):
    return SavedAgentStore(store, settings)


def create(agents, *, schedule=DAILY, enabled=True, **config):
    return agents.create(name="a", config={**CONFIG, **config}, schedule=schedule, enabled=enabled)


def make_due(agents, agent_id, seconds_ago=1):
    with agents.engine.begin() as db:
        db.execute(update(saved_agents).where(saved_agents.c.id == agent_id).values(next_run=now() - timedelta(seconds=seconds_ago)))
    return agents.get(agent_id)["next_run"]


def runs_of(agents, agent_id):
    with agents.engine.connect() as db:
        return [dict(r) for r in db.execute(select(runs).where(runs.c.agent_id == agent_id)).mappings()]


def test_enabling_a_schedule_never_runs_immediately_or_retroactively(agents):
    agent = create(agents)
    assert agent["next_run"] > now()
    assert agents.run_due() == [] and runs_of(agents, agent["id"]) == []


def test_a_due_occurrence_starts_one_run_and_advances(agents):
    agent = create(agents)
    due = make_due(agents, agent["id"])
    (run_id,) = agents.run_due()
    (run,) = runs_of(agents, agent["id"])
    assert run["id"] == run_id and run["status"] == "running" and run["scheduled_for"] == due and run["mode"] == "test"
    after = agents.get(agent["id"])
    assert after["next_run"] > now() and after["last_run_id"] == run_id and after["last_error"] == ""
    assert agents.run_due() == []  # the same occurrence is never started twice


def test_disabled_agents_do_not_fire(agents):
    agent = create(agents, enabled=False)
    assert agent["next_run"] is None
    with agents.engine.begin() as db:
        db.execute(update(saved_agents).where(saved_agents.c.id == agent["id"]).values(next_run=now() - timedelta(seconds=5)))
    assert agents.run_due() == []


def test_replicas_never_start_the_same_occurrence_twice(settings, store, agents):
    agent = create(agents)
    make_due(agents, agent["id"])
    replicas = [SavedAgentStore(RunStore(make_engine(settings), settings), settings) for _ in range(4)]
    barrier, started = threading.Barrier(len(replicas)), []

    def tick(replica):
        barrier.wait()
        started.extend(replica.run_due())

    threads = [threading.Thread(target=tick, args=(r,)) for r in replicas]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    assert len(started) == 1 and len(runs_of(agents, agent["id"])) == 1
    for replica in replicas:
        replica.engine.dispose()


def test_overlap_waits_for_the_previous_run_within_the_grace_period(agents):
    agent = create(agents)
    make_due(agents, agent["id"])
    (first,) = agents.run_due()
    due = make_due(agents, agent["id"], seconds_ago=10)  # next occurrence arrives while `first` still runs
    assert agents.run_due() == [] and agents.get(agent["id"])["next_run"] == due  # waits, same as the previous system
    agents.runs.update(first, status="completed", result="done")
    (second,) = agents.run_due()
    assert agents.runs.get(second)["scheduled_for"] == due and len(runs_of(agents, agent["id"])) == 2


def test_overlap_that_outlasts_the_grace_period_counts_as_missed(agents):
    agent = create(agents)
    make_due(agents, agent["id"])
    agents.run_due()
    make_due(agents, agent["id"], seconds_ago=MISSED_GRACE_SECONDS + 5)
    assert agents.run_due() == [] and agents.get(agent["id"])["last_error"] == "schedule_missed"
    assert len(runs_of(agents, agent["id"])) == 1


def test_a_manual_active_run_also_blocks_the_schedule(agents):
    agent = create(agents)
    agents.start_now(agent["id"])
    due = make_due(agents, agent["id"], seconds_ago=10)
    assert agents.run_due() == [] and agents.get(agent["id"])["next_run"] == due


def test_late_occurrences_are_skipped_not_replayed(agents):
    agent = create(agents)
    make_due(agents, agent["id"], seconds_ago=3 * 24 * 3600)  # three days of downtime
    assert agents.run_due() == []
    after = agents.get(agent["id"])
    assert after["last_error"] == "schedule_missed" and after["next_run"] > now()
    assert runs_of(agents, agent["id"]) == [] and agents.run_due() == []  # no catch-up burst


def test_full_slots_make_it_wait_within_the_grace_period_then_count_as_missed(agents, settings):
    agent = create(agents)
    due = make_due(agents, agent["id"], seconds_ago=10)
    blockers = [agents.runs.reserve_run(prompt="b", model="m", mode="test", language="en", tools=[], files=[]) for _ in range(settings.max_concurrent_runs)]
    assert agents.run_due() == []
    assert agents.get(agent["id"])["next_run"] == due  # waits, not skipped
    agents.runs.cancel(blockers[0]["id"])
    (run_id,) = agents.run_due()
    assert agents.runs.get(run_id)["scheduled_for"] == due
    # Same situation again, but past the grace period: missed.
    agent2 = create(agents)
    make_due(agents, agent2["id"], seconds_ago=MISSED_GRACE_SECONDS + 5)
    assert agents.run_due() == [] and agents.get(agent2["id"])["last_error"] == "schedule_missed"
    with pytest.raises(Busy):  # the started run plus two blockers fill every slot again
        agents.start_now(agent["id"])


def test_a_failed_start_is_not_retried_before_the_next_occurrence(settings, store):
    broken = SavedAgentStore(store, settings.model_copy(update={"default_model": ""}))
    agent = broken.create(name="live", config={**CONFIG, "mode": "live"}, schedule=DAILY, enabled=True)  # no model resolvable
    make_due(broken, agent["id"])
    assert broken.run_due() == []
    after = broken.get(agent["id"])
    assert after["last_error"] == "schedule_start_failed" and after["next_run"] > now()
    assert runs_of(broken, agent["id"]) == [] and broken.run_due() == []


def test_the_model_is_resolved_when_the_run_starts(agents, settings):
    agent = create(agents, mode="live", model=None)
    assert agents.start_now(agent["id"])["model"] == settings.default_model
    explicit = create(agents, mode="live", model="claude-alias")
    assert agents.start_now(explicit["id"])["model"] == "claude-alias"


def test_update_keeps_an_unchanged_next_run_and_recomputes_a_changed_one(agents):
    agent = create(agents)
    same = agents.update(agent["id"], revision=1, name="renamed", config=CONFIG, schedule=DAILY, enabled=True)
    assert same["next_run"] == agent["next_run"] and same["revision"] == 2 and same["name"] == "renamed"
    changed = agents.update(agent["id"], revision=2, name="renamed", config=CONFIG, schedule=Schedule(kind="hourly", minute=7), enabled=True)
    assert changed["next_run"] != agent["next_run"] and changed["next_run"] > now()


def test_pause_and_resume_do_not_catch_up(agents):
    agent = create(agents)
    paused = agents.update(agent["id"], revision=1, name="a", config=CONFIG, schedule=DAILY, enabled=False)
    assert paused["next_run"] is None and paused["schedule"] == DAILY.canonical()
    resumed = agents.update(agent["id"], revision=2, name="a", config=CONFIG, schedule=DAILY, enabled=True)
    assert resumed["next_run"] > now() and agents.run_due() == []


def test_stale_revisions_and_missing_agents(agents):
    agent = create(agents)
    agents.update(agent["id"], revision=1, name="x", config=CONFIG, schedule=DAILY, enabled=True)
    with pytest.raises(RevisionConflict):
        agents.update(agent["id"], revision=1, name="y", config=CONFIG, schedule=DAILY, enabled=True)
    assert agents.get(agent["id"])["name"] == "x"
    with pytest.raises(AgentNotFound):
        agents.update("0" * 8 + "-0000-0000-0000-" + "0" * 12, revision=1, name="z", config=CONFIG, schedule=None, enabled=False)
    with pytest.raises(AgentNotFound):
        agents.start_now("0" * 8 + "-0000-0000-0000-" + "0" * 12)


def test_delete_stops_the_schedule_but_keeps_runs(agents):
    agent = create(agents, enabled=False)
    run = agents.start_now(agent["id"])
    assert agents.delete(agent["id"]) and not agents.delete(agent["id"])
    assert agents.get(agent["id"]) is None and agents.runs.get(run["id"])["agent_id"] == agent["id"]


def test_agent_limit(agents):
    for _ in range(MAX_AGENTS):
        agents.create(name="a", config=CONFIG, schedule=None, enabled=False)
    with pytest.raises(AgentLimit):
        agents.create(name="one too many", config=CONFIG, schedule=None, enabled=False)


def test_scheduler_tick_submits_started_runs(agents):
    agent = create(agents)
    make_due(agents, agent["id"])
    submitted = []
    scheduler = Scheduler(agents, submitted.append, 1)
    started = scheduler.tick()
    assert started == submitted and len(started) == 1
    assert scheduler.tick() == []


def record_call(agents, *, status, error="", seconds_ago=60):
    agents.runs.record_llm_call(
        run_id="r", call_id="c", model="m", status=status, error=error,
        started=now() - timedelta(seconds=seconds_ago), duration_ms=1,
    )


def test_a_recent_rate_limit_pauses_scheduled_starts_but_not_manual_ones(agents):
    agent = create(agents)
    record_call(agents, status="error", error="error_rate_limited")
    make_due(agents, agent["id"])
    assert agents.run_due() == []
    after = agents.get(agent["id"])
    assert after["last_error"] == "schedule_paused" and after["next_run"] > now()  # consumed, never replayed
    assert runs_of(agents, agent["id"]) == []
    assert agents.start_now(agent["id"])["status"] == "running"  # manual starts are never blocked


def test_the_pause_ends_after_a_success_the_cooldown_or_when_disabled(agents, settings, store):
    agent = create(agents)
    record_call(agents, status="error", error="error_rate_limited", seconds_ago=120)
    record_call(agents, status="ok", seconds_ago=60)  # the gateway answered again
    make_due(agents, agent["id"])
    assert len(agents.run_due()) == 1

    old = SavedAgentStore(store, settings.model_copy(update={"rate_limit_cooldown_seconds": 30}))
    cooled = old.create(name="b", config=CONFIG, schedule=DAILY, enabled=True)
    record_call(agents, status="error", error="error_rate_limited", seconds_ago=3600)  # older than 30 s cooldown
    make_due(old, cooled["id"])
    assert len(old.run_due()) == 1

    off = SavedAgentStore(store, settings.model_copy(update={"rate_limit_cooldown_seconds": 0}))
    never = off.create(name="c", config=CONFIG, schedule=DAILY, enabled=True)
    record_call(agents, status="error", error="error_rate_limited", seconds_ago=1)
    make_due(off, never["id"])
    assert len(off.run_due()) == 1


def test_manual_start_records_the_run_and_clears_the_last_error(agents):
    agent = create(agents)
    make_due(agents, agent["id"], seconds_ago=MISSED_GRACE_SECONDS + 5)
    agents.run_due()
    assert agents.get(agent["id"])["last_error"] == "schedule_missed"
    run = agents.start_now(agent["id"])
    after = agents.get(agent["id"])
    assert after["last_run_id"] == run["id"] and after["last_error"] == ""


def test_editing_clears_the_last_error(agents):
    agent = create(agents)
    make_due(agents, agent["id"], seconds_ago=MISSED_GRACE_SECONDS + 5)
    agents.run_due()
    edited = agents.update(agent["id"], revision=1, name="a", config=CONFIG, schedule=DAILY, enabled=True)
    assert edited["last_error"] == ""


def test_missing_attached_files_block_the_start(agents, store):
    from deep_agents_app.saved.store import FilesMissing

    store.add_input_file("a" * 32, "x.txt", 1, "0" * 64)
    agent = create(agents, tools=["files"], files=["a" * 32])
    assert agents.start_now(agent["id"])["files"] == ["a" * 32]
    gone = create(agents, tools=["files"], files=["b" * 32])  # never uploaded
    with pytest.raises(FilesMissing):
        agents.start_now(gone["id"])
    make_due(agents, gone["id"])
    assert agents.run_due() == [] and agents.get(gone["id"])["last_error"] == "schedule_start_failed"
