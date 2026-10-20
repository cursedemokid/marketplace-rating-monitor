"""Ежедневный прогон работает по завершённым московским суткам."""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import zoneinfo
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from app import __main__ as cli
from app import jobs
from app.clock import ManualClock
from app.config import Secrets, Settings, load_settings
from app.db.locking import DAILY_LOCK
from app.services.collect import CollectResult
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

MOSCOW = zoneinfo.ZoneInfo("Europe/Moscow")


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args: object) -> None:
        pass

    async def commit(self) -> None:
        pass


def _patch_daily_stages(
    monkeypatch: pytest.MonkeyPatch, state: dict[str, bool] | None = None
) -> dict[str, bool]:
    from app.repositories.runs import RunRepository

    stages = (
        state
        if state is not None
        else {
            "collect": False,
            "classify": False,
            "analyze": False,
            "notify": False,
        }
    )

    async def get_stage(_self: object, _date: dt.date) -> dict[str, bool]:
        return stages.copy()

    async def mark_stage(_self: object, _date: dt.date, stage: str) -> None:
        stages[stage] = True

    async def record_classify_status(
        _self: object, _date: dt.date, status: str, _detail: str | None = None
    ) -> None:
        stages["classify"] = status in {"complete", "partial", "disabled"}

    monkeypatch.setattr(RunRepository, "get_daily_stages", get_stage)
    monkeypatch.setattr(RunRepository, "mark_daily_stage", mark_stage)
    monkeypatch.setattr(
        RunRepository, "record_classify_status", record_classify_status, raising=False
    )
    monkeypatch.setattr(jobs, "build_session_factory", lambda _engine: lambda: _FakeSession())
    return stages


async def test_daily_runs_all_stages_for_previous_completed_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock(dt.datetime(2026, 10, 1, 9, 0, tzinfo=MOSCOW), MOSCOW)
    settings = load_settings()
    settings = settings.model_copy(
        update={"ai": settings.ai.model_copy(update={"automatic_enabled": False})}
    )
    secrets = Secrets.model_construct(database_url="postgresql+asyncpg://unused")
    calls: list[tuple[str, dt.date | None]] = []

    async def collect(
        _settings: object, _secrets: object, _clock: object, date: dt.date
    ) -> CollectResult:
        calls.append(("collect", date))
        return CollectResult(1, 1, 0, 1, 0)

    async def analyze(_secrets: object, _clock: object, date: dt.date) -> None:
        calls.append(("analyze", date))

    async def notify(_settings: object, _secrets: object, _clock: object) -> object:
        calls.append(("notify", None))
        return SimpleNamespace(failed=0)

    monkeypatch.setattr(jobs, "collect", collect)
    monkeypatch.setattr(jobs, "analyze", analyze)
    _patch_daily_stages(monkeypatch)
    monkeypatch.setattr(jobs, "notify", notify)

    @asynccontextmanager
    async def available_lock(*args: object):
        yield True

    monkeypatch.setattr(jobs, "try_job_lock", available_lock)

    await jobs.daily(settings, secrets, clock)

    assert calls == [
        ("collect", dt.date(2026, 9, 30)),
        ("analyze", dt.date(2026, 9, 30)),
        ("notify", None),
    ]


async def test_daily_does_not_analyze_incomplete_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock(dt.datetime(2026, 10, 1, 9, 0, tzinfo=MOSCOW), MOSCOW)
    settings = load_settings()
    secrets = Secrets.model_construct(database_url="postgresql+asyncpg://unused")
    calls: list[str] = []

    @asynccontextmanager
    async def available_lock(*args: object):
        yield True

    async def collect(*args: object) -> CollectResult:
        calls.append("collect")
        return CollectResult(1, 1, 0, 1, 0, failed_products=1, errors=("rating unavailable",))

    async def analyze(*args: object) -> None:
        calls.append("analyze")

    async def notify(*args: object) -> object:
        calls.append("notify")
        return SimpleNamespace(failed=0)

    monkeypatch.setattr(jobs, "try_job_lock", available_lock)
    monkeypatch.setattr(jobs, "collect", collect)
    monkeypatch.setattr(jobs, "analyze", analyze)
    monkeypatch.setattr(jobs, "notify", notify)
    _patch_daily_stages(monkeypatch)

    with pytest.raises(jobs.IncompleteCollectionError):
        await jobs.daily(settings, secrets, clock)

    assert calls == ["collect"]


async def test_daily_retries_from_first_unfinished_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock(dt.datetime(2026, 10, 1, 9, 0, tzinfo=MOSCOW), MOSCOW)
    settings = load_settings()
    settings = settings.model_copy(
        update={"ai": settings.ai.model_copy(update={"automatic_enabled": False})}
    )
    secrets = Secrets.model_construct(database_url="postgresql+asyncpg://unused")
    date = dt.date(2026, 9, 30)
    complete = {"collect": False, "classify": False, "analyze": False, "notify": False}
    calls: list[str] = []
    notification_attempts = 0

    class FakeEngine:
        async def dispose(self) -> None:
            pass

    @asynccontextmanager
    async def available_lock(*args: object):
        yield True

    async def collect(*args: object) -> CollectResult:
        calls.append("collect")
        return CollectResult(1, 1, 0, 1, 0)

    async def analyze(*args: object) -> None:
        calls.append("analyze")
        if calls.count("analyze") == 1:
            raise RuntimeError("temporary analyze failure")

    async def notify(*args: object) -> object:
        nonlocal notification_attempts
        calls.append("notify")
        notification_attempts += 1
        return SimpleNamespace(failed=1 if notification_attempts == 1 else 0)

    monkeypatch.setattr(jobs, "build_engine", lambda *_args: FakeEngine())
    monkeypatch.setattr(jobs, "try_job_lock", available_lock)
    monkeypatch.setattr(jobs, "collect", collect)
    monkeypatch.setattr(jobs, "analyze", analyze)
    monkeypatch.setattr(jobs, "notify", notify)
    _patch_daily_stages(monkeypatch, complete)

    with pytest.raises(RuntimeError, match="temporary analyze failure"):
        await jobs.daily_for_date(settings, secrets, clock, date)
    assert complete == {"collect": True, "classify": True, "analyze": False, "notify": False}

    assert await jobs.daily_for_date(settings, secrets, clock, date)
    assert complete == {"collect": True, "classify": True, "analyze": True, "notify": False}
    assert await jobs.daily_for_date(settings, secrets, clock, date)
    assert complete == {"collect": True, "classify": True, "analyze": True, "notify": True}
    assert calls == ["collect", "analyze", "analyze", "notify", "notify"]


async def test_daily_classifies_before_analysis_and_continues_when_budget_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.repositories.ai_cost import CostLimitExceededError

    clock = ManualClock(dt.datetime(2026, 10, 1, 9, 0, tzinfo=MOSCOW), MOSCOW)
    loaded = load_settings()
    settings = loaded.model_copy(
        update={
            "ai": loaded.ai.model_copy(update={"automatic_enabled": True}),
        }
    )
    secrets = Secrets.model_construct(database_url="postgresql+asyncpg://unused")
    calls: list[str] = []

    @asynccontextmanager
    async def available_lock(*_args: object):
        yield True

    async def collect(*_args: object) -> CollectResult:
        calls.append("collect")
        return CollectResult(1, 1, 0, 1, 0)

    async def classify(*_args: object, **_kwargs: object) -> None:
        calls.append("classify")
        raise CostLimitExceededError("monthly cap")

    async def analyze(*_args: object) -> None:
        calls.append("analyze")

    async def notify(*_args: object) -> object:
        calls.append("notify")
        return SimpleNamespace(failed=0)

    class FakeEngine:
        async def dispose(self) -> None:
            pass

    monkeypatch.setattr(jobs, "build_engine", lambda *_args: FakeEngine())
    monkeypatch.setattr(jobs, "try_job_lock", available_lock)
    monkeypatch.setattr(jobs, "collect", collect)
    monkeypatch.setattr(jobs, "classify", classify)
    monkeypatch.setattr(jobs, "analyze", analyze)
    monkeypatch.setattr(jobs, "notify", notify)
    stages = _patch_daily_stages(monkeypatch)

    assert await jobs.daily_for_date(settings, secrets, clock, dt.date(2026, 9, 30))
    assert calls == ["collect", "classify", "analyze", "notify"]
    assert stages == {"collect": True, "classify": False, "analyze": True, "notify": True}

    assert await jobs.daily_for_date(settings, secrets, clock, dt.date(2026, 9, 30))
    assert calls == ["collect", "classify", "analyze", "notify", "classify"]


async def test_daily_continues_analysis_after_fatal_provider_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock(dt.datetime(2026, 10, 1, 9, 0, tzinfo=MOSCOW), MOSCOW)
    loaded = load_settings()
    settings = loaded.model_copy(
        update={"ai": loaded.ai.model_copy(update={"automatic_enabled": True})}
    )
    secrets = Secrets.model_construct(database_url="postgresql+asyncpg://unused")
    calls: list[str] = []

    @asynccontextmanager
    async def available_lock(*_args: object):
        yield True

    async def classify(*_args: object, **_kwargs: object) -> None:
        calls.append("classify")
        response = httpx.Response(401, request=httpx.Request("POST", "https://internal.example.invalid/endpoint"))
        raise httpx.HTTPStatusError("unauthorized", request=response.request, response=response)

    async def collect(*_args: object) -> CollectResult:
        calls.append("collect")
        return CollectResult(1, 1, 0, 1, 0)

    async def analyze(*_args: object) -> None:
        calls.append("analyze")

    async def notify(*_args: object) -> object:
        calls.append("notify")
        return SimpleNamespace(failed=0)

    class FakeEngine:
        async def dispose(self) -> None:
            pass

    monkeypatch.setattr(jobs, "build_engine", lambda *_args: FakeEngine())
    monkeypatch.setattr(jobs, "try_job_lock", available_lock)
    monkeypatch.setattr(jobs, "collect", collect)
    monkeypatch.setattr(jobs, "classify", classify)
    monkeypatch.setattr(jobs, "analyze", analyze)
    monkeypatch.setattr(jobs, "notify", notify)
    stages = _patch_daily_stages(monkeypatch)

    assert await jobs.daily_for_date(settings, secrets, clock, dt.date(2026, 9, 30))
    assert calls == ["collect", "classify", "analyze", "notify"]
    assert stages == {"collect": True, "classify": False, "analyze": True, "notify": True}


def test_scheduler_fires_at_configured_moscow_time() -> None:
    from app.scheduler import build_scheduler, catch_up

    settings = load_settings()
    secrets = Secrets.model_construct(database_url="postgresql+asyncpg://unused")
    clock = ManualClock(dt.datetime(2026, 10, 1, 8, 0, tzinfo=MOSCOW), MOSCOW)

    scheduler = build_scheduler(settings, secrets, clock)
    job = scheduler.get_job("daily")

    assert job is not None
    assert job.func is catch_up
    assert job.max_instances == 1
    assert job.trigger.get_next_fire_time(None, clock.now()) == dt.datetime(
        2026, 10, 1, 9, 0, tzinfo=MOSCOW
    )


def test_due_date_waits_until_configured_moscow_time() -> None:
    from app.scheduler import due_collection_date

    clock = ManualClock(dt.datetime(2026, 10, 15, 8, 59, tzinfo=MOSCOW), MOSCOW)
    assert due_collection_date(clock, "09:00") == dt.date(2026, 10, 13)
    clock.advance(dt.timedelta(minutes=1))
    assert due_collection_date(clock, "09:00") == dt.date(2026, 10, 14)


def test_missing_dates_replay_gaps_after_first_success() -> None:
    from app.scheduler import missing_collection_dates

    assert missing_collection_dates(
        latest=dt.date(2026, 10, 17),
        complete={dt.date(2026, 10, 13), dt.date(2026, 10, 15)},
        baseline_days=14,
    ) == [dt.date(2026, 10, 14), dt.date(2026, 10, 16), dt.date(2026, 10, 17)]


def test_missing_dates_bootstrap_only_yesterday_and_limit_old_gaps() -> None:
    from app.scheduler import missing_collection_dates

    latest = dt.date(2026, 10, 31)
    assert missing_collection_dates(latest=latest, complete=set(), baseline_days=14) == [latest]
    dates = missing_collection_dates(
        latest=latest, complete={dt.date(2026, 10, 1)}, baseline_days=14
    )
    assert dates[0] == dt.date(2026, 10, 16)
    assert dates[-1] == latest
    assert len(dates) == 16


async def test_catch_up_runs_missing_days_in_order(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories.runs import RunRepository
    from app.scheduler import catch_up

    clock = ManualClock(dt.datetime(2026, 10, 18, 10, tzinfo=MOSCOW), MOSCOW)
    secrets = Secrets.model_construct(database_url=os.environ["DATABASE_URL"])
    settings = load_settings()
    days: list[dt.date] = []

    async def complete_dates(
        _self: RunRepository, account_keys: tuple[str, ...], *, until: dt.date
    ) -> set[dt.date]:
        assert account_keys == ("default", "DemoSeller")
        assert until == dt.date(2026, 10, 17)
        return {dt.date(2026, 10, 13), dt.date(2026, 10, 15)}

    async def run_day(_settings: object, _secrets: object, _clock: object, day: dt.date) -> bool:
        days.append(day)
        return True

    monkeypatch.setattr(RunRepository, "complete_dates", complete_dates)
    monkeypatch.setattr(
        jobs,
        "resolve_wb_profiles",
        lambda _secrets: (SimpleNamespace(key="default"), SimpleNamespace(key="DemoSeller")),
    )
    monkeypatch.setattr(jobs, "daily_for_date", run_day)

    await catch_up(settings, secrets, clock)
    assert days == [dt.date(2026, 10, 14), dt.date(2026, 10, 16), dt.date(2026, 10, 17)]


async def test_scheduler_catches_up_before_entering_daily_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.scheduler as scheduler

    events: list[str] = []
    started = asyncio.Event()

    async def fake_catch_up(*_args: object) -> None:
        events.append("catch-up")

    class FakeScheduler:
        def start(self) -> None:
            events.append("start")
            started.set()

        def shutdown(self, *, wait: bool) -> None:
            assert not wait
            events.append("stop")

    monkeypatch.setattr(scheduler, "catch_up", fake_catch_up)
    monkeypatch.setattr(scheduler, "build_scheduler", lambda *_args: FakeScheduler())
    settings = load_settings()
    secrets = Secrets.model_construct(database_url="postgresql+asyncpg://unused")
    clock = ManualClock(dt.datetime(2026, 10, 18, 10, tzinfo=MOSCOW), MOSCOW)

    task = asyncio.create_task(scheduler.run_forever(settings, secrets, clock))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["catch-up", "start", "stop"]


@pytest.mark.parametrize("run_at", ["25:00", "09:60", "9:00", "09:00:00"])
def test_invalid_schedule_time_is_rejected_at_config_load(run_at: str) -> None:
    raw = load_settings().model_dump()
    raw["collector"]["run_at"] = run_at

    with pytest.raises(ValidationError):
        Settings.model_validate(raw)


def test_unimplemented_email_channel_is_rejected_at_config_load() -> None:
    raw = load_settings().model_dump()
    raw["notify"]["channel"] = "email"

    with pytest.raises(ValidationError):
        Settings.model_validate(raw)


@pytest.mark.parametrize("command", ["collect", "analyze"])
async def test_manual_command_refuses_while_daily_run_holds_lock(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    @asynccontextmanager
    async def unavailable_lock(_engine: object, key: int):
        assert key == DAILY_LOCK
        yield False

    async def collect(*_args: object) -> CollectResult:
        calls.append("collect")
        return CollectResult(1, 1, 0, 1, 0)

    async def analyze(*_args: object) -> None:
        calls.append("analyze")

    monkeypatch.setattr(jobs, "try_job_lock", unavailable_lock)
    monkeypatch.setattr(jobs, "collect", collect)
    monkeypatch.setattr(jobs, "analyze", analyze)
    monkeypatch.setattr(
        cli,
        "load_secrets",
        lambda: Secrets.model_construct(database_url="postgresql+asyncpg://unused"),
    )
    monkeypatch.setattr(cli, "setup_logging", lambda **_kwargs: None)

    with pytest.raises(RuntimeError, match="уже выполняется"):
        await cli._run(cli._parse_args([command, "--date", "DemoPhone"]))

    assert calls == []


@pytest.mark.parametrize("command", ["collect", "analyze"])
async def test_manual_command_keeps_lock_until_stage_finishes(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"locked": False, "called": False}

    @asynccontextmanager
    async def available_lock(_engine: object, key: int):
        assert key == DAILY_LOCK
        state["locked"] = True
        try:
            yield True
        finally:
            state["locked"] = False

    async def collect(*_args: object) -> CollectResult:
        assert state["locked"]
        state["called"] = True
        return CollectResult(1, 1, 0, 1, 0)

    async def analyze(*_args: object) -> None:
        assert state["locked"]
        state["called"] = True

    monkeypatch.setattr(jobs, "try_job_lock", available_lock)
    monkeypatch.setattr(jobs, "collect", collect)
    monkeypatch.setattr(jobs, "analyze", analyze)
    monkeypatch.setattr(
        cli,
        "load_secrets",
        lambda: Secrets.model_construct(database_url="postgresql+asyncpg://unused"),
    )
    monkeypatch.setattr(cli, "setup_logging", lambda **_kwargs: None)

    await cli._run(cli._parse_args([command, "--date", "DemoPhone"]))

    assert state == {"locked": False, "called": True}
