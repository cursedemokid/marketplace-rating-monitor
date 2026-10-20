"""Проверка свежести полного ночного сбора для внешнего мониторинга."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import Clock


@dataclass(frozen=True, slots=True)
class CollectionHealth:
    expected_date: dt.date
    healthy: bool


@dataclass(frozen=True, slots=True)
class AccountHistory:
    account_key: str
    monitored: int
    ready: int
    required_days: int


async def history_coverage(
    session: AsyncSession,
    *,
    on_date: dt.date,
    baseline_days: int,
    account_keys: tuple[str, ...],
) -> list[AccountHistory]:
    """Товары с непрерывной историей: текущий день, база и день до базы."""
    required = baseline_days + 2
    start = on_date - dt.timedelta(days=required - 1)
    result = await session.execute(
        text("""
            with product_days as (
                select p.account_key, p.nm_id,
                       count(s.snapshot_date) as observed
                  from wb.products p
                  left join wb.daily_snapshots s
                    on s.nm_id = p.nm_id
                   and s.snapshot_date between :start and :on_date
                 where p.is_monitored
                   and p.account_key = any(:account_keys)
                 group by p.account_key, p.nm_id
            )
            select account_key, count(*) as monitored,
                   count(*) filter (where observed = :required) as ready
              from product_days
             group by account_key
             order by account_key
        """),
        {
            "start": start,
            "on_date": on_date,
            "account_keys": list(account_keys),
            "required": required,
        },
    )
    return [
        AccountHistory(str(row.account_key), int(row.monitored), int(row.ready), required)
        for row in result.all()
    ]


async def check_collection(
    session: AsyncSession,
    clock: Clock,
    *,
    run_at: str,
    grace_hours: int,
    account_keys: tuple[str, ...] = ("default",),
) -> CollectionHealth:
    """После окна завершения требовать успешный прогон со снимком за вчера."""
    hour, minute = (int(value) for value in run_at.split(":"))
    deadline = dt.datetime.combine(
        clock.today(), dt.time(hour, minute), tzinfo=clock.tz
    ) + dt.timedelta(hours=grace_hours)
    days_ago = 1 if clock.now() >= deadline else 2
    expected = clock.today() - dt.timedelta(days=days_ago)
    for account_key in account_keys:
        result = await session.execute(
            text("""
            select exists (
                select 1 from wb.collection_runs
                 where run_date = :expected
                   and account_key = :account_key
                   and status = 'ok'
                   and finished_at is not null
            ) and exists (
                select 1 from wb.daily_snapshots s
                  join wb.products p on p.nm_id = s.nm_id
                 where s.snapshot_date = :expected and p.account_key = :account_key
            )
        """),
            {"expected": expected, "account_key": account_key},
        )
        if not result.scalar_one():
            return CollectionHealth(expected_date=expected, healthy=False)
    return CollectionHealth(expected_date=expected, healthy=True)
