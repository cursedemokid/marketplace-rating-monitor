"""Журнал сбора не оставляет вечные записи running после прерывания процесса."""

from __future__ import annotations

import datetime as dt

from app.repositories.runs import RunRepository
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def test_restart_marks_previous_unfinished_run_as_failed(session: AsyncSession) -> None:
    repo = RunRepository(session)
    run_date = dt.date(2026, 10, 13)
    first = await repo.start(run_date, dt.datetime(2026, 10, 14, 9, tzinfo=dt.UTC), "default")
    second = await repo.start(run_date, dt.datetime(2026, 10, 14, 10, tzinfo=dt.UTC), "default")

    rows = await session.execute(
        text(
            "select id, status, finished_at, error from wb.collection_runs "
            "where id in (:first, :second) order by id"
        ),
        {"first": first, "second": second},
    )
    previous, current = rows.all()
    assert previous.id == first
    assert previous.status == "failed"
    assert previous.finished_at is not None
    assert "прерван" in previous.error.lower()
    assert current.id == second
    assert current.status == "running"
    assert current.finished_at is None


async def test_complete_dates_require_success_for_both_accounts(session: AsyncSession) -> None:
    repo = RunRepository(session)
    started = dt.datetime(2099, 1, 5, 9, tzinfo=dt.UTC)
    complete_day = dt.date(2099, 1, 4)
    partial_day = dt.date(2099, 1, 5)
    for day, account, status in (
        (complete_day, "default", "ok"),
        (complete_day, "DemoSeller", "ok"),
        (partial_day, "default", "ok"),
        (partial_day, "DemoSeller", "partial"),
    ):
        run_id = await repo.start(day, started, account)
        await repo.finish(run_id, finished_at=started, status=status)

    assert await repo.complete_dates(("default", "DemoSeller"), until=partial_day) == {complete_day}


async def test_daily_stages_persist_and_find_unfinished_dates(session: AsyncSession) -> None:
    repo = RunRepository(session)
    day = dt.date(2099, 2, 5)
    assert await repo.get_daily_stages(day) == {
        "collect": False,
        "classify": False,
        "analyze": False,
        "notify": False,
    }
    for stage in ("collect", "classify", "analyze"):
        await repo.mark_daily_stage(day, stage)
    assert day in await repo.unfinished_daily_dates(until=day)
    await repo.mark_daily_stage(day, "notify")
    assert day not in await repo.unfinished_daily_dates(until=day)
    assert all((await repo.get_daily_stages(day)).values())


async def test_failed_collection_before_first_success_is_scheduled_for_catch_up(
    session: AsyncSession,
) -> None:
    repo = RunRepository(session)
    started = dt.datetime(2099, 3, 3, 9, tzinfo=dt.UTC)
    failed_day = dt.date(2099, 3, 1)
    successful_day = dt.date(2099, 3, 2)
    failed_run = await repo.start(failed_day, started, "default")
    await repo.finish(failed_run, finished_at=started, status="partial")
    successful_run = await repo.start(successful_day, started, "default")
    await repo.finish(successful_run, finished_at=started, status="ok")

    assert failed_day in await repo.unfinished_daily_dates(until=successful_day)
