"""Межпроцессные блокировки долгих задач через PostgreSQL."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

DAILY_LOCK = 20_260_101


@asynccontextmanager
async def try_job_lock(engine: AsyncEngine, key: int) -> AsyncIterator[bool]:
    """Держать блокировку до выхода из контекста; отказ возвращается сразу.

    Блокировка привязана к транзакции, поэтому работает и через transaction
    pooler и автоматически снимается при потере соединения.
    """
    async with engine.connect() as conn:
        acquired = bool(
            (
                await conn.execute(text("select pg_try_advisory_xact_lock(:key)"), {"key": key})
            ).scalar_one()
        )
        try:
            yield acquired
        finally:
            await conn.rollback()
