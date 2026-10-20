"""Ручная выгрузка принимает период и несколько артикулов."""

from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from app import __main__ as cli
from app import jobs
from app.__main__ import _collection_target, _parse_args
from app.config import ConfigError, Secrets
from app.services.collect import CollectResult


def test_export_command_parses_period_skus_and_output() -> None:
    args = _parse_args(
        [
            "export-ratings",
            "--start",
            "DemoPhone",
            "--end",
            "DemoPhone",
            "--nm-id",
            "123",
            "--nm-id",
            "456",
            "--output",
            "tmp/rating.xlsx",
        ]
    )

    assert args.start == dt.date(2026, 9, 10)
    assert args.end == dt.date(2026, 9, 11)
    assert args.nm_ids == [123, 456]
    assert args.output == Path("tmp/rating.xlsx")


def test_reason_export_command_parses_period_and_products() -> None:
    args = _parse_args(
        ["export-reasons", "--start", "DemoPhone", "--end", "DemoPhone", "--nm-id", "123"]
    )
    assert args.command == "export-reasons"
    assert args.start == dt.date(2026, 9, 10)
    assert args.end == dt.date(2026, 9, 11)
    assert args.nm_ids == [123]


def test_local_user_creation_command_parses_role() -> None:
    args = _parse_args(["add-user", "--username", "viewer1", "--role", "viewer"])
    assert args.command == "add-user"
    assert args.username == "viewer1"
    assert args.role == "viewer"


def test_quality_review_commands_parse_source_and_reviewer() -> None:
    review = _parse_args(["quality-review", "--input", "sample.csv", "--output", "review.xlsx"])
    approval = _parse_args(
        [
            "quality-approve",
            "--input",
            "review.xlsx",
            "--source",
            "sample.csv",
            "--reviewer",
            "owner",
            "--output",
            "approved",
        ]
    )
    assert review.input == Path("sample.csv")
    assert approval.source == Path("sample.csv")
    assert approval.reviewer == "owner"


async def test_classify_retry_flag_reaches_paid_job(monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def available_lock(_secrets: Secrets):
        yield

    received: dict[str, object] = {}

    async def classify_job(*_args: object, **kwargs: object) -> None:
        received.update(kwargs)

    monkeypatch.setattr(jobs, "classify", classify_job)
    monkeypatch.setattr(jobs, "manual_run_lock", available_lock)
    monkeypatch.setattr(
        cli,
        "load_secrets",
        lambda: Secrets(_env_file=None, database_url="postgresql+asyncpg://ignored"),
    )

    await cli._run(
        _parse_args(
            ["classify", "--until", "DemoPhone", "--limit", "21", "--input", "ids.csv", "--retry"]
        )
    )
    assert received["retry"] is True


async def test_classify_manual_uncertain_flag_reaches_paid_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def available_lock(_secrets: Secrets):
        yield

    received: dict[str, object] = {}

    async def classify_job(*_args: object, **kwargs: object) -> None:
        received.update(kwargs)

    monkeypatch.setattr(jobs, "classify", classify_job)
    monkeypatch.setattr(jobs, "manual_run_lock", available_lock)
    monkeypatch.setattr(
        cli,
        "load_secrets",
        lambda: Secrets(_env_file=None, database_url="postgresql+asyncpg://ignored"),
    )

    await cli._run(
        _parse_args(
            ["classify", "--until", "DemoPhone", "--limit", "1", "--allow-uncertain-manual"]
        )
    )

    assert received["allow_uncertain_manual"] is True


def test_partial_manual_collection_exits_with_error(monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def available_lock(_secrets: Secrets):
        yield

    async def partial(*args: object) -> CollectResult:
        return CollectResult(
            products=4,
            feedbacks=60,
            skipped_feedbacks=0,
            snapshots=4,
            rechecked_dates=7,
            failed_products=4,
            errors=("DemoSeller: категория Аналитика недоступна",),
        )

    monkeypatch.setattr(jobs, "collect", partial)
    monkeypatch.setattr(jobs, "manual_run_lock", available_lock)
    monkeypatch.setattr(
        cli,
        "load_secrets",
        lambda: Secrets(_env_file=None, database_url="postgresql+asyncpg://ignored"),
    )

    assert cli.main(["collect", "--date", "DemoPhone"]) == 1


def test_interrupted_collection_returns_nonzero_status(monkeypatch: pytest.MonkeyPatch) -> None:
    async def interrupted(_args: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_run", interrupted)

    assert cli.main(["collect", "--date", "DemoPhone"]) == 130


def test_collect_defaults_to_last_completed_day() -> None:
    assert _collection_target(None, dt.date(2026, 10, 15)) == dt.date(2026, 10, 14)
    assert _collection_target(dt.date(2026, 10, 13), dt.date(2026, 10, 15)) == dt.date(2026, 10, 13)


def test_collect_rejects_current_or_future_day() -> None:
    for requested in (dt.date(2026, 10, 15), dt.date(2026, 10, 16)):
        with pytest.raises(ConfigError, match="текущий день"):
            _collection_target(requested, dt.date(2026, 10, 15))
