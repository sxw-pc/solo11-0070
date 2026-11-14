"""Injectable clock used to make runs, reports and tests deterministic."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone


def format_utc(value: datetime) -> str:
    """Render a UTC datetime in the compact format stored in SQLite."""

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: str) -> datetime:
    """Parse an ISO-8601 instant, treating a missing zone as UTC."""

    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class Clock:
    """Small clock interface with sleeping attached for deterministic tests."""

    def now(self) -> datetime:
        raise NotImplementedError

    def monotonic(self) -> float:
        raise NotImplementedError

    def sleep(self, seconds: float) -> None:
        raise NotImplementedError

    def now_iso(self) -> str:
        return format_utc(self.now())


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class FixedClock(Clock):
    """A manually advanced clock. Sleeping advances both time sources."""

    def __init__(self, start_iso: str = "2024-01-01T00:00:00Z", monotonic_start: float = 1000.0):
        self._now = parse_utc(start_iso)
        self._monotonic = float(monotonic_start)
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self.sleeps.append(float(seconds))
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        delta = timedelta(seconds=float(seconds))
        self._now = self._now + delta
        self._monotonic += float(seconds)

    def set(self, iso_value: str) -> None:
        self._now = parse_utc(iso_value)

    @property
    def total_slept(self) -> float:
        return sum(self.sleeps)
