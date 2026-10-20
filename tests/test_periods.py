"""Периоды сравнения и приросты по снимкам. TSK-202, AC-2."""

from __future__ import annotations

import datetime as dt

from app.services.periods import Period, counts_for_period, previous_period, rolling_baseline
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

NM_ID = 999_000_003


def test_previous_period_of_one_day() -> None:
    """День к дню: предыдущий период — вчера."""
    day = Period(dt.date(2026, 9, 24), dt.date(2026, 9, 24))
    assert previous_period(day) == Period(dt.date(2026, 9, 23), dt.date(2026, 9, 23))


def test_previous_period_of_a_week() -> None:
    """Неделя к неделе: семь дней, вплотную перед выбранными."""
    week = Period(dt.date(2026, 9, 18), dt.date(2026, 9, 24))
    assert week.days == 7
    assert previous_period(week) == Period(dt.date(2026, 9, 11), dt.date(2026, 9, 17))


def test_previous_period_of_arbitrary_interval() -> None:
    """Произвольный интервал — предшествующий интервал равной длительности."""
    period = Period(dt.date(2026, 9, 10), dt.date(2026, 9, 24))
    previous = previous_period(period)
    assert previous.days == period.days
    assert previous.end == dt.date(2026, 9, 9)


def test_rolling_baseline_ends_yesterday() -> None:
    """Базовое окно детектора не включает сам анализируемый день."""
    base = rolling_baseline(dt.date(2026, 9, 24), days=14)
    assert base.end == dt.date(2026, 9, 23)
    assert base.start == dt.date(2026, 9, 10)
    assert base.days == 14


async def test_counts_need_snapshot_before_period(session: AsyncSession) -> None:
    """Нет снимка за день перед периодом — артикула нет в результате.

    Это и есть признак «нет данных» из AC-2. Ноль оценок и отсутствие
    данных — разные вещи, и подставлять вместо второго первое нельзя.
    """
    await session.execute(
        text("""
            insert into wb.products (nm_id, vendor_code, updated_at)
            values (:nm, 'TEST-PERIOD', now())
            on conflict (nm_id) do nothing
        """),
        {"nm": NM_ID},
    )
    await session.execute(
        text("""
            insert into wb.daily_snapshots (
                nm_id, snapshot_date, rating, ratings_total,
                ratings_5, ratings_4, ratings_3, ratings_2, ratings_1, collected_at
            ) values (:nm, :d, 4.9, 100, 90, 5, 3, 1, 1, now())
        """),
        {"nm": NM_ID, "d": dt.date(2026, 9, 24)},
    )

    only_end = await counts_for_period(session, Period(dt.date(2026, 9, 24), dt.date(2026, 9, 24)))
    assert NM_ID not in only_end


async def test_counts_are_increments_not_totals(session: AsyncSession) -> None:
    """Прирост за период, а не накопленный итог. design § D5, TSK-201."""
    await session.execute(
        text("""
            insert into wb.products (nm_id, vendor_code, updated_at)
            values (:nm, 'TEST-PERIOD', now())
            on conflict (nm_id) do nothing
        """),
        {"nm": NM_ID},
    )
    await session.execute(
        text("""
            insert into wb.daily_snapshots (
                nm_id, snapshot_date, rating, ratings_total,
                ratings_5, ratings_4, ratings_3, ratings_2, ratings_1,
                buyout_percent, orders_count, collected_at
            ) values
                (:nm, :before, 4.90, 100, 90, 5, 3, 1, 1, 87.0, 500, now()),
                (:nm, :day,    4.88, 120, 95, 7, 8, 5, 5, 86.5, 520, now())
        """),
        {"nm": NM_ID, "before": dt.date(2026, 9, 23), "day": dt.date(2026, 9, 24)},
    )

    counts = await counts_for_period(session, Period(dt.date(2026, 9, 24), dt.date(2026, 9, 24)))
    row = counts[NM_ID]
    assert row.total == 20, "за день пришло 20 оценок, а не 120"
    assert row.five == 5
    assert row.negative == 4 + 4 + 5, "негатив — это 1, 2 и 3 вместе"
    assert row.rating_before == 4.90
    assert row.rating_after == 4.88
    assert row.buyout_before == 87.0
    assert row.buyout_after == 86.5
