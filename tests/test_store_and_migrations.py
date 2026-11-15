from datetime import timedelta

import pytest
from sqlalchemy import inspect, text, update

from deep_agents_app.db.engine import make_engine
from deep_agents_app.db.migrate import migrate
from deep_agents_app.db.schema import runs
from deep_agents_app.runs.store import Busy, EventLimit, RunStore, now

from .conftest import make_settings


def reserve(store, **kw):
    return store.reserve_run(prompt="p", model="m", mode="test", language="en", tools=[], files=[], **kw)


def test_migration_creates_schema_and_is_repeatable(settings):
    engine = make_engine(settings)
    migrate(settings, engine)
    migrate(settings, engine)
    tables = set(inspect(engine).get_table_names(schema=settings.database_schema))
    assert {"runs", "run_events", "tool_audit", "input_files", "artifacts", "alembic_version", "checkpoints"} <= tables
    engine.dispose()


def test_run_slots_are_limited(store):
    ids = [reserve(store)["id"] for _ in range(store.settings.max_concurrent_runs)]
    with pytest.raises(Busy):
        reserve(store)
    assert store.cancel(ids[0])
    assert reserve(store)["status"] == "running"


def test_orphaned_runs_are_reaped_without_resubmission(store):
    run = reserve(store)
    old = now() - timedelta(seconds=store.settings.run_timeout_seconds + 120)
    with store.engine.begin() as db:
        db.execute(update(runs).where(runs.c.id == run["id"]).values(created=old))
    store.reap()
    assert store.get(run["id"])["status"] == "failed" and store.get(run["id"])["error"] == "run_orphaned"


def test_cancelled_runs_stay_cancelled(store):
    run = reserve(store)
    assert store.cancel(run["id"])
    assert not store.update(run["id"], status="completed", result="late")
    assert store.get(run["id"])["status"] == "cancelled"


def test_events_are_ordered_projected_and_bounded(store, monkeypatch):
    run = reserve(store)
    first = store.append_event(run["id"], "metadata", {"a": 1})
    store.append_event(run["id"], "values", {"messages": [{"type": "human", "content": "hi"}]})
    events = store.events_after(run["id"], first)
    assert [e["event"] for e in events] == ["values"]
    assert store.get(run["id"])["messages"][0]["content"] == "hi"
    assert store.last_event_id(run["id"]) == events[-1]["id"]
    monkeypatch.setattr("deep_agents_app.runs.store.MAX_EVENTS", 2)
    with pytest.raises(EventLimit):
        store.append_event(run["id"], "updates", {})


def test_two_instances_share_the_slot_limit(settings, store):
    other = RunStore(make_engine(settings), settings)
    for _ in range(settings.max_concurrent_runs):
        reserve(store)
    with pytest.raises(Busy):
        reserve(other)
    other.engine.dispose()


def test_other_schema_is_isolated(database_url, tmp_path, store):
    reserve(store)
    other_settings = make_settings(database_url, "t_other_" + store.settings.database_schema[2:10], tmp_path)
    engine = make_engine(other_settings)
    migrate(other_settings, engine)
    assert RunStore(engine, other_settings).list_runs() == []
    with engine.begin() as db:
        db.execute(text(f'DROP SCHEMA "{other_settings.database_schema}" CASCADE'))
    engine.dispose()
