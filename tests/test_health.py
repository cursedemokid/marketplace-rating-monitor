"""Свежесть суточного сбора для внешнего мониторинга."""

from __future__ import annotations

import datetime as dt
import os

import pytest
from app import jobs
from app.clock import ManualClock
from app.config import Secrets, load_settings
from app.repositories.products import ProductRepository, ProductRow
from app.repositories.snapshots import SnapshotRepository, SnapshotRow
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def test_health_requires_successful_run_and_snapshot_after_deadline(
    session: AsyncSession, clock: ManualClock
) -> None:
    from app.services.health import check_collection

    clock.set(dt.datetime(2026, 10, 1, 11, 1, tzinfo=clock.tz))
    expected = dt.date(2026, 9, 30)

    missing = await check_collection(session, clock, run_at="09:00", grace_hours=2)
    assert missing.expected_date == expected
    assert not missing.healthy

    await ProductRepository(session).upsert_many(
        [ProductRow(999_000_909, "HEALTH-TEST", None, None, None)]
    )
    await session.execute(
        text("""
            insert into wb.collection_runs (run_date, started_at, finished_at, status)
            values (:d, :started, :finished, 'partial')
        """),
        {"d": expected, "started": clock.now(), "finished": clock.now()},
    )
    await session.execute(
        text("""
            insert into wb.daily_snapshots (
                nm_id, snapshot_date, rating, ratings_total, ratings_5,
                ratings_4, ratings_3, ratings_2, ratings_1, collected_at
            ) values (1676122247, :d, 4.8, 1, 1, 0, 0, 0, 0, now())
        """),
        {"d": expected},
    )

    partial = await check_collection(session, clock, run_at="09:00", grace_hours=2)
    assert not partial.healthy

    await session.execute(
        text("""
            insert into wb.collection_runs (run_date, started_at, finished_at, status)
            values (:d, :started, :finished, 'ok')
        """),
        {"d": expected, "started": clock.now(), "finished": clock.now()},
    )
    complete = await check_collection(session, clock, run_at="09:00", grace_hours=2)
    assert complete.healthy
    both = await check_collection(
        session,
        clock,
        run_at="09:00",
        grace_hours=2,
        account_keys=("default", "DemoSeller"),
    )
    assert not both.healthy


async def test_health_waits_for_configured_completion_window(
    session: AsyncSession, clock: ManualClock
) -> None:
    from app.services.health import check_collection

    clock.set(dt.datetime(2026, 10, 1, 10, 59, tzinfo=clock.tz))
    state = await check_collection(session, clock, run_at="09:00", grace_hours=2)

    assert state.expected_date == dt.date(2026, 9, 29)


async def test_health_command_reports_missing_collection(
    session: AsyncSession, clock: ManualClock
) -> None:
    clock.set(dt.datetime(2026, 10, 1, 11, 1, tzinfo=clock.tz))
    secrets = Secrets.model_construct(database_url=os.environ["DATABASE_URL"])

    with pytest.raises(jobs.StaleCollectionError):
        await jobs.health(load_settings(), secrets, clock)


async def test_history_coverage_requires_day_before_baseline(
    session: AsyncSession,
) -> None:
    from app.services.health import history_coverage

    await ProductRepository(session).upsert_many(
        [
            ProductRow(999_000_911, "HISTORY-A", None, None, None, account_key="history_a"),
            ProductRow(999_000_912, "HISTORY-B", None, None, None, account_key="history_b"),
        ]
    )
    rows = [
        SnapshotRow(
            nm_id=nm_id,
            snapshot_date=day,
            rating=4.8,
            ratings_total=1,
            ratings_5=1,
            ratings_4=0,
            ratings_3=0,
            ratings_2=0,
            ratings_1=0,
            buyout_percent=None,
            orders_count=None,
            buyouts_count=None,
        )
        for nm_id, days in (
            (999_000_911, (8, 9, 10)),
            (999_000_912, (9, 10)),
        )
        for day in (dt.date(2026, 10, day) for day in days)
    ]
    await SnapshotRepository(session).upsert_many(rows)

    result = await history_coverage(
        session,
        on_date=dt.date(2026, 10, 10),
        baseline_days=1,
        account_keys=("history_a", "history_b"),
    )
    assert [(item.account_key, item.monitored, item.ready) for item in result] == [
        ("history_a", 1, 1),
        ("history_b", 1, 0),
    ]
    assert all(item.required_days == 3 for item in result)
