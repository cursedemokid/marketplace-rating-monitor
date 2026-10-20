"""Провайдер времени.

Прямой вызов `datetime.now()` по коду запрещён: AC-4 (начисление возобновляется
на следующие сутки) и AC-9 (закрытие периода) иначе нельзя прогнать в тестах
быстрее реальных суток. Всё время идёт через Clock, в тестах подменяется
на ManualClock.

Часовой пояс — из period.timezone (FR-23, NFR-5): сутки и месяц считаются
в Europe/Moscow, а хранится всё в timestamptz.
"""

from __future__ import annotations

import datetime as dt
import zoneinfo
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    @property
    def tz(self) -> zoneinfo.ZoneInfo:
        """Пояс, в котором считаются сутки и границы периода."""

    def now(self) -> dt.datetime:
        """Текущий момент, aware, в поясе `tz`."""

    def today(self) -> dt.date:
        """Текущая календарная дата в поясе `tz`."""


class SystemClock:
    """Боевые часы: системное время, приведённое к нужному поясу."""

    __slots__ = ("_tz",)

    def __init__(self, tz: zoneinfo.ZoneInfo) -> None:
        self._tz = tz

    @property
    def tz(self) -> zoneinfo.ZoneInfo:
        return self._tz

    def now(self) -> dt.datetime:
        return dt.datetime.now(tz=self._tz)

    def today(self) -> dt.date:
        return self.now().date()


class ManualClock:
    """Часы для тестов: стоят на месте, пока их не двинут."""

    __slots__ = ("_now", "_tz")

    def __init__(self, moment: dt.datetime, tz: zoneinfo.ZoneInfo) -> None:
        if moment.tzinfo is None:
            msg = "ManualClock требует aware datetime, naive приведёт к ошибке на границе суток"
            raise ValueError(msg)
        self._tz = tz
        self._now = moment.astimezone(tz)

    @property
    def tz(self) -> zoneinfo.ZoneInfo:
        return self._tz

    def now(self) -> dt.datetime:
        return self._now

    def today(self) -> dt.date:
        return self._now.date()

    def advance(self, delta: dt.timedelta) -> None:
        self._now = (self._now + delta).astimezone(self._tz)

    def set(self, moment: dt.datetime) -> None:
        if moment.tzinfo is None:
            msg = "ManualClock.set требует aware datetime"
            raise ValueError(msg)
        self._now = moment.astimezone(self._tz)
