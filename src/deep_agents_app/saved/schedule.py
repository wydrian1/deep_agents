"""Pure schedule rules: when does an agent run next?

* ``hourly``: every 3600 real seconds, at the configured minute of the local clock. UTC offsets of
  whole hours (and the usual half/quarter-hour zones) keep the minute stable, and a DST change can
  never skip or repeat an hour (a 23-hour day has 23 runs, a 25-hour day 25).
* ``daily`` / ``weekly``: at a local wall-clock time in ``SCHEDULE_TIMEZONE`` (Monday = 0). A wall
  time that does not exist (spring gap) moves forward by the gap (02:30 -> 03:30); a wall time that
  occurs twice (autumn) runs only the first time. They follow calendar days, not 24/168-hour counters.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

Kind = Literal["hourly", "daily", "weekly"]


class Schedule(BaseModel):
    kind: Kind
    minute: int = Field(default=0, ge=0, le=59)
    hour: int = Field(default=0, ge=0, le=23)  # ignored for hourly
    weekday: int = Field(default=0, ge=0, le=6)  # Monday = 0; weekly only

    def canonical(self) -> dict[str, int | str]:
        """Only the fields that matter for this kind, so equal rules compare equal."""
        data: dict[str, int | str] = {"kind": self.kind, "minute": self.minute}
        if self.kind != "hourly":
            data["hour"] = self.hour
        if self.kind == "weekly":
            data["weekday"] = self.weekday
        return data


def _resolve_local(naive: datetime, tz: ZoneInfo) -> datetime:
    """UTC instant of a local wall time: gap times move forward, repeated times use the first occurrence."""
    return naive.replace(tzinfo=tz, fold=0).astimezone(UTC)


def next_occurrence(schedule: Schedule, after: datetime, tz: ZoneInfo) -> datetime:
    """The first occurrence strictly after ``after`` (timezone-aware, returned in UTC)."""
    after = after.astimezone(UTC)
    if schedule.kind == "hourly":
        offset = after.astimezone(tz).utcoffset() or timedelta(0)
        utc_minute = (schedule.minute - int(offset.total_seconds() // 60)) % 60
        candidate = after.replace(minute=utc_minute, second=0, microsecond=0)
        return candidate if candidate > after else candidate + timedelta(hours=1)
    local_after = after.astimezone(tz)
    for days in range(0, 9):
        day = local_after.date() + timedelta(days=days)
        if schedule.kind == "weekly" and day.weekday() != schedule.weekday:
            continue
        candidate = _resolve_local(datetime(day.year, day.month, day.day, schedule.hour, schedule.minute), tz)
        if candidate > after:
            return candidate
    raise ValueError("no next occurrence")  # unreachable for valid schedules
