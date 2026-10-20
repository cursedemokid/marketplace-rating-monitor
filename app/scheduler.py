"""Запуск ежедневной цепочки по московскому расписанию."""

from __future__ import annotations

import asyncio
import datetime as dt

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from app import jobs
from app.clock import Clock
from app.config import Secrets, Settings
from app.db.engine import build_engine
from app.db.session import build_session_factory
from app.repositories.runs import RunRepository
from app.repositories.signals import SignalRepository


def due_collection_date(clock: Clock, run_at: str) -> dt.date:
    """Последние сутки, сбор которых уже должен был стартовать по расписанию."""
    hour, minute = (int(part) for part in run_at.split(":"))
    now = clock.now()
    today = now.date()
    scheduled = dt.datetime.combine(today, dt.time(hour, minute), tzinfo=clock.tz)
    return today - dt.timedelta(days=1 if now >= scheduled else 2)


def missing_collection_dates(
    *, latest: dt.date, complete: set[dt.date], baseline_days: int
) -> list[dt.date]:
    """После первого успешного дня заполнить дыры в окне базы сравнения."""
    if not complete:
        return [latest]
    # Для прироста за базовое окно нужен также снимок перед его первым днём.
    start = max(min(complete), latest - dt.timedelta(days=max(1, baseline_days) + 1))
    return [
        day
        for offset in range((latest - start).days + 1)
        if (day := start + dt.timedelta(days=offset)) not in complete
    ]


async def catch_up(settings: Settings, secrets: Secrets, clock: Clock) -> None:
    """При старте догнать пропущенные дни до запуска постоянного расписания."""
    account_keys = tuple(profile.key for profile in jobs.resolve_wb_profiles(secrets))
    engine = build_engine(secrets.database_url)
    factory = build_session_factory(engine)
    try:
        while True:
            latest = due_collection_date(clock, settings.collector.run_at)
            async with factory() as session:
                complete = await RunRepository(session).complete_dates(account_keys, until=latest)
                unfinished = await RunRepository(session).unfinished_daily_dates(until=latest)
                signal_settings = await SignalRepository(session).settings()
            baseline_days = max(
                (item.baseline_days for item in signal_settings.values() if item.is_enabled),
                default=14,
            )
            missing = missing_collection_dates(
                latest=latest, complete=complete, baseline_days=baseline_days
            )
            missing = sorted(set(missing) | unfinished)
            for day in missing:
                logger.info("Восстановление пропущенного сбора за {}", day)
                if not await jobs.daily_for_date(settings, secrets, clock, day):
                    raise jobs.JobAlreadyRunningError(
                        f"Не удалось догнать {day}: сбор выполняется другим процессом"
                    )
            if due_collection_date(clock, settings.collector.run_at) == latest:
                logger.info("История сбора актуальна по {} включительно", latest)
                return
    finally:
        await engine.dispose()


def build_scheduler(settings: Settings, secrets: Secrets, clock: Clock) -> AsyncIOScheduler:
    """Зарегистрировать один ежедневный прогон без наложения внутри процесса."""
    hour, minute = (int(part) for part in settings.collector.run_at.split(":"))
    scheduler = AsyncIOScheduler(timezone=clock.tz)
    scheduler.add_job(
        catch_up,
        trigger=CronTrigger(hour=hour, minute=minute, timezone=clock.tz),
        args=[settings, secrets, clock],
        id="daily",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    return scheduler


async def run_forever(settings: Settings, secrets: Secrets, clock: Clock) -> None:
    """Держать планировщик живым до остановки процесса."""
    await catch_up(settings, secrets, clock)
    scheduler = build_scheduler(settings, secrets, clock)
    scheduler.start()
    logger.info("Ежедневный прогон назначен на {} ({})", settings.collector.run_at, clock.tz)
    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown(wait=False)
