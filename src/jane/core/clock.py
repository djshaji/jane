"""Injected time; no sleeps or wall-clock dependence in scenario replay."""

import math
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    @property
    def session_id(self) -> str: ...

    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class FakeClock:
    def __init__(self, start: datetime, session_id: str = "demo-session") -> None:
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("start must be timezone-aware")
        if not session_id:
            raise ValueError("session ID must not be empty")
        self._start = start.astimezone(UTC)
        self._elapsed = 0.0
        self._session_id = session_id

    @property
    def session_id(self) -> str:
        return self._session_id

    def now(self) -> datetime:
        return self._start + timedelta(seconds=self._elapsed)

    def monotonic(self) -> float:
        return self._elapsed

    def advance(self, seconds: float) -> None:
        if isinstance(seconds, bool) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError("advance must be finite and nonnegative")
        elapsed = self._elapsed + seconds
        # Validate representability before mutating either time representation.
        self._start + timedelta(seconds=elapsed)
        self._elapsed = elapsed
