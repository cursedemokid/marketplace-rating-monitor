"""Блокировки между процессами на настоящей PostgreSQL."""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import AsyncIterator

import pytest
from app import jobs
from app.clock import ManualClock
from app.config import Secrets, load_settings
from app.db.engine import build_engine
from app.db.locking import DAILY_LOCK, try_job_lock
from app.db.session import build_session_factory
from app.repositories.feedbacks import FeedbackRepository, FeedbackRow
from app.repositories.products import ProductRepository, ProductRow
from app.repositories.reasons import ReasonRepository
from app.repositories.signals import SignalRepository, SignalRow
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("нет DATABASE_URL")
    db = build_engine(url)
    try:
        yield db
    finally:
        await db.dispose()


async def test_second_process_cannot_take_daily_lock_until_first_releases(
    engine: AsyncEngine,
) -> None:
    async with try_job_lock(engine, DAILY_LOCK) as first:
        assert first
        async with try_job_lock(engine, DAILY_LOCK) as second:
            assert not second
    async with try_job_lock(engine, DAILY_LOCK) as third:
        assert third


async def test_daily_skips_stages_when_another_process_holds_lock(
    engine: AsyncEngine, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def collect(*args: object) -> None:
        calls.append("collect")

    async def analyze(*args: object) -> None:
        calls.append("analyze")

    async def notify(*args: object) -> None:
        calls.append("notify")

    monkeypatch.setattr(jobs, "collect", collect)
    monkeypatch.setattr(jobs, "analyze", analyze)
    monkeypatch.setattr(jobs, "notify", notify)
    secrets = Secrets.model_construct(database_url=os.environ["DATABASE_URL"])

    async with try_job_lock(engine, DAILY_LOCK):
        await jobs.daily(load_settings(), secrets, clock)

    assert calls == []


async def test_second_sender_skips_signal_locked_by_first(
    engine: AsyncEngine, clock: ManualClock
) -> None:
    factory = build_session_factory(engine)
    async with factory() as setup:
        await ProductRepository(setup).upsert_many(
            [ProductRow(999_000_888, "LOCK-TEST", None, None, None)]
        )
        signal_id = await SignalRepository(setup).add(
            SignalRow(
                nm_id=999_000_888,
                signal_type="rating_change",
                period_start=dt.date(2026, 9, 23),
                period_end=dt.date(2026, 9, 23),
                value_before=4.9,
                value_after=4.8,
                delta=-0.1,
                payload={},
            ),
            created_at=clock.now(),
        )
        await setup.commit()

    try:
        async with factory() as first, factory() as second:
            first_rows = await SignalRepository(first).outbox(limit=1, after_id=signal_id - 1)
            assert [row.id for row in first_rows] == [signal_id]

            second_rows = await SignalRepository(second).outbox(limit=1, after_id=signal_id - 1)
            assert signal_id not in [row.id for row in second_rows]

            await first.rollback()
            retry_rows = await SignalRepository(second).outbox(limit=1, after_id=signal_id - 1)
            assert [row.id for row in retry_rows] == [signal_id]
    finally:
        async with factory() as cleanup:
            await cleanup.execute(text("delete from wb.signals where id = :id"), {"id": signal_id})
            await cleanup.execute(text("delete from wb.products where nm_id = 2974425828"))
            await cleanup.commit()


async def test_second_classifier_skips_review_locked_by_first(engine: AsyncEngine) -> None:
    factory = build_session_factory(engine)
    feedback_id = "classify-lock-test"
    async with factory() as setup:
        await ProductRepository(setup).upsert_many(
            [ProductRow(999_000_889, "CLASSIFY-LOCK", None, None, None)]
        )
        await FeedbackRepository(setup).upsert_many(
            [
                FeedbackRow(
                    id=feedback_id,
                    nm_id=999_000_889,
                    valuation=1,
                    text="протекает",
                    pros="",
                    cons="",
                    created_at=dt.datetime(2026, 9, 24, tzinfo=dt.UTC),
                    created_date=dt.date(2026, 9, 24),
                )
            ]
        )
        await setup.commit()

    try:
        async with factory() as first, factory() as second:
            first_rows = await ReasonRepository(first).pending(
                until=dt.date(2026, 9, 24), max_stars=3, limit=1
            )
            assert [row.feedback_id for row in first_rows] == [feedback_id]

            second_rows = await ReasonRepository(second).pending(
                until=dt.date(2026, 9, 24), max_stars=3, limit=1
            )
            assert feedback_id not in [row.feedback_id for row in second_rows]
    finally:
        async with factory() as cleanup:
            await cleanup.execute(
                text("delete from wb.feedbacks where id = :id"), {"id": feedback_id}
            )
            await cleanup.execute(text("delete from wb.products where nm_id = 4608770698"))
            await cleanup.commit()
