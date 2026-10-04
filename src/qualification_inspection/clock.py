"""可注入时钟。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Protocol


class Clock(Protocol):
    def today(self) -> date: ...


class SystemClock:
    """系统时钟（生产环境）。"""

    def today(self) -> date:
        return datetime.now().date()


class FixedClock:
    """固定时钟（测试与回放）。"""

    def __init__(self, day: date | str) -> None:
        self._day = day if isinstance(day, date) else date.fromisoformat(day)

    def today(self) -> date:
        return self._day

    def advance_to(self, day: date | str) -> None:
        self._day = day if isinstance(day, date) else date.fromisoformat(day)

    def advance(self, days: int) -> date:
        from datetime import timedelta

        self._day += timedelta(days=days)
        return self._day
