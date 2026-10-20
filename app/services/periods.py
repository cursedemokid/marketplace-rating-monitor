"""Периоды сравнения и приросты по снимкам. FR-2.1, FR-2.2, FR-2.4, TSK-202.

Две базы сравнения, и это не дублирование:

* **предыдущий период равной длины** — для отчётов и дашборда, буквально
  по ТЗ п. 3 и FR-2.4: день к дню, неделя к неделе, месяц к месяцу;
* **скользящее окно** — внутри детектора сигналов. Дневное сравнение при
  10–20 оценках в сутки шумит: вчерашние случайные две единицы делают
  сегодняшнюю норму «падением». Окно в две недели даёт устойчивую базу,
  и это не противоречит ТЗ, а выполняет его п. 4 — учитывать размер выборки.

Приросты считаются по снимкам, которые хранят накопленные счётчики:

    прирост за [a, b] = счётчики(b) − счётчики(a − 1 день)

Отсюда требование к данным: чтобы посчитать период, нужен снимок за день
ПЕРЕД его началом. Нет его — возвращаем «нет данных», а не нули (AC-2).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class Period:
    start: dt.date
    end: dt.date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def __str__(self) -> str:
        return f"{self.start:%d.%m.%Y}—{self.end:%d.%m.%Y}"


@dataclass(frozen=True, slots=True)
class PeriodCounts:
    """Прирост оценок за период и показатели на его конец."""

    nm_id: int
    period: Period
    total: int
    five: int
    four: int
    three: int
    two: int
    one: int
    rating_before: float | None
    rating_after: float | None
    buyout_before: float | None
    buyout_after: float | None
    orders: int | None

    @property
    def negative(self) -> int:
        """Оценки 1, 2 и 3 — негатив по определению ТЗ п. 8."""
        return self.one + self.two + self.three


def previous_period(period: Period) -> Period:
    """Предыдущий интервал такой же длины непосредственно перед выбранным.

    Для отчётов и дашборда (FR-2.4). Неделя к неделе, месяц к месяцу —
    это он же, просто с интервалом другой длины.
    """
    length = period.days
    end = period.start - dt.timedelta(days=1)
    return Period(start=end - dt.timedelta(days=length - 1), end=end)


def rolling_baseline(on_date: dt.date, days: int) -> Period:
    """Окно `days` дней, заканчивающееся накануне `on_date`.

    База для детектора сигналов: сравниваем сегодняшний приток оценок
    со средним фоном последних двух недель, а не со вчерашним днём.
    """
    end = on_date - dt.timedelta(days=1)
    return Period(start=end - dt.timedelta(days=days - 1), end=end)


_COUNTS = text("""
    select s_end.nm_id,
           s_end.ratings_total - s_before.ratings_total as total,
           s_end.ratings_5     - s_before.ratings_5     as five,
           s_end.ratings_4     - s_before.ratings_4     as four,
           s_end.ratings_3     - s_before.ratings_3     as three,
           s_end.ratings_2     - s_before.ratings_2     as two,
           s_end.ratings_1     - s_before.ratings_1     as one,
           s_before.rating         as rating_before,
           s_end.rating            as rating_after,
           s_before.buyout_percent as buyout_before,
           s_end.buyout_percent    as buyout_after,
           s_end.orders_count      as orders
      from wb.daily_snapshots s_end
      join wb.daily_snapshots s_before
        on s_before.nm_id = s_end.nm_id
       and s_before.snapshot_date = :before
     where s_end.snapshot_date = :end_date
""")


async def counts_for_period(
    session: AsyncSession,
    period: Period,
) -> dict[int, PeriodCounts]:
    """Прирост оценок за период по каждому артикулу.

    Артикулы, у которых нет снимка за день перед началом периода или за его
    конец, в результат не попадают вовсе — это и есть признак «нет данных».
    Молча подставлять нули нельзя: ноль оценок и отсутствие данных —
    разные вещи, и на первом сигналы строить можно, а на втором нет.
    """
    before = period.start - dt.timedelta(days=1)
    result = await session.execute(_COUNTS, {"before": before, "end_date": period.end})
    return {
        int(row.nm_id): PeriodCounts(
            nm_id=int(row.nm_id),
            period=period,
            total=int(row.total),
            five=int(row.five),
            four=int(row.four),
            three=int(row.three),
            two=int(row.two),
            one=int(row.one),
            rating_before=float(row.rating_before) if row.rating_before is not None else None,
            rating_after=float(row.rating_after) if row.rating_after is not None else None,
            buyout_before=float(row.buyout_before) if row.buyout_before is not None else None,
            buyout_after=float(row.buyout_after) if row.buyout_after is not None else None,
            orders=int(row.orders) if row.orders is not None else None,
        )
        for row in result.all()
    }
