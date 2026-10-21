from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


def utc(dt: datetime) -> datetime:
    # assume naive = UTC
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


class Clock(Protocol):
    @property
    def utc_now(self) -> datetime: ...


class SystemClock:
    @property
    def utc_now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    def __init__(self, start: datetime | None = None):
        self._now = utc(start or datetime(2025, 5, 13, 8, 0, tzinfo=UTC))

    @property
    def utc_now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta

    def set(self, instant: datetime) -> None:
        self._now = utc(instant)
