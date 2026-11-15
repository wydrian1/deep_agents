from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from deep_agents_app.saved.schedule import Schedule, next_occurrence

BUDAPEST = ZoneInfo("Europe/Budapest")
UTC_ = UTC


def at(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC_)


def local(moment, tz=BUDAPEST):
    return moment.astimezone(tz)


def test_validation():
    with pytest.raises(ValidationError):
        Schedule(kind="daily", minute=60)
    with pytest.raises(ValidationError):
        Schedule(kind="weekly", weekday=7)
    with pytest.raises(ValidationError):
        Schedule(kind="monthly")  # type: ignore[arg-type]


def test_canonical_keeps_only_relevant_fields():
    assert Schedule(kind="hourly", minute=5, hour=9, weekday=3).canonical() == {"kind": "hourly", "minute": 5}
    assert Schedule(kind="daily", minute=5, hour=9, weekday=3).canonical() == {"kind": "daily", "minute": 5, "hour": 9}
    assert Schedule(kind="weekly", minute=5, hour=9, weekday=3).canonical() == {"kind": "weekly", "minute": 5, "hour": 9, "weekday": 3}


def test_daily_is_strictly_after_and_uses_the_local_clock():
    rule = Schedule(kind="daily", hour=8, minute=30)
    first = next_occurrence(rule, at(2026, 11, 15, 6, 0), BUDAPEST)  # 07:00 local, before 08:30
    assert local(first).strftime("%Y-%m-%d %H:%M") == "2026-11-15 08:30"
    assert local(next_occurrence(rule, first, BUDAPEST)).strftime("%Y-%m-%d %H:%M") == "2026-11-16 08:30"  # strictly after


def test_weekly_uses_monday_zero():
    rule = Schedule(kind="weekly", weekday=0, hour=9)
    result = local(next_occurrence(rule, at(2026, 11, 15, 12), BUDAPEST))  # a Sunday
    assert (result.strftime("%A %H:%M"), result.day) == ("Monday 09:00", 16)
    assert local(next_occurrence(rule, result.astimezone(UTC_), BUDAPEST)).day == 23


def test_spring_gap_moves_forward_by_the_gap():
    rule = Schedule(kind="daily", hour=2, minute=30)  # 2027-03-28 02:30 does not exist in Budapest
    result = local(next_occurrence(rule, at(2027, 3, 27, 12), BUDAPEST))
    assert result.strftime("%Y-%m-%d %H:%M") == "2027-03-28 03:30"


def test_autumn_repeated_time_runs_only_once():
    rule = Schedule(kind="daily", hour=2, minute=30)  # 2026-10-25 02:30 happens twice
    first = next_occurrence(rule, at(2026, 10, 24, 12), BUDAPEST)
    assert first == at(2026, 10, 25, 0, 30)  # the first 02:30 (CEST)
    second = next_occurrence(rule, first, BUDAPEST)
    assert local(second).strftime("%Y-%m-%d %H:%M") == "2026-10-26 02:30"  # not the repeated 02:30 CET (01:30Z)


@pytest.mark.parametrize(("day", "expected"), [((2026, 6, 10), 24), ((2027, 3, 28), 23), ((2026, 10, 25), 25)])
def test_hourly_fires_once_per_real_hour_even_across_dst(day, expected):
    rule = Schedule(kind="hourly", minute=15)
    start = datetime(*day, tzinfo=BUDAPEST).astimezone(UTC_) - timedelta(seconds=1)
    end = datetime(*day, tzinfo=BUDAPEST) + timedelta(days=1)
    count, cursor, previous = 0, start, None
    while True:
        cursor = next_occurrence(rule, cursor, BUDAPEST)
        if local(cursor).replace(tzinfo=None) >= end.replace(tzinfo=None) and local(cursor).date() != datetime(*day).date():
            break
        if local(cursor).date() == datetime(*day).date():
            count += 1
        assert local(cursor).minute == 15
        assert previous is None or cursor - previous == timedelta(hours=1)
        previous = cursor
    assert count == expected


def test_hourly_in_a_half_hour_zone_keeps_the_local_minute():
    kolkata = ZoneInfo("Asia/Kolkata")
    result = next_occurrence(Schedule(kind="hourly", minute=15), at(2026, 11, 15, 10, 0), kolkata)
    assert local(result, kolkata).minute == 15 and result > at(2026, 11, 15, 10, 0)
    assert next_occurrence(Schedule(kind="hourly", minute=15), result, kolkata) - result == timedelta(hours=1)
