"""Общие фикстуры. Фикстуры ответов Wildberries лежат в tests/fixtures/wb/."""

from __future__ import annotations

import datetime as dt
import os
import zoneinfo
from collections.abc import AsyncIterator

import pytest
from app.clock import ManualClock
from app.db.engine import build_engine
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MOSCOW = zoneinfo.ZoneInfo("Europe/Moscow")


@pytest.fixture
def clock() -> ManualClock:
    """Часы стоят на месте: тесты на границе суток должны быть воспроизводимы."""
    return ManualClock(dt.datetime(2026, 9, 24, 9, 0, tzinfo=MOSCOW), MOSCOW)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("нет DATABASE_URL — интеграционные тесты пропущены")
    engine = build_engine(url)
    try:
        async with engine.connect() as connection:
            outer = await connection.begin()
            try:
                async with AsyncSession(
                    bind=connection, join_transaction_mode="create_savepoint"
                ) as db:
                    try:
                        await db.execute(text("select 1 from wb.products limit 1"))
                    except Exception:
                        pytest.skip("схема не применена: uv run python -m app.migrate")
                    yield db
            finally:
                await outer.rollback()
    finally:
        await engine.dispose()
