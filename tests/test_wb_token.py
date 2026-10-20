"""Боевой сбор требует нужные категории; клиент ограничивает операции чтением."""

from __future__ import annotations

import base64
import datetime as dt
import json

import pytest
from app import jobs
from app.adapters.wb.token import WbTokenAccessError, require_collection_token
from app.clock import SystemClock
from app.config import Secrets, load_settings


def token(*, scope: int, account: int = 3, test: bool = False) -> str:
    payload = {"s": scope, "acc": account, "t": test}
    middle = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"e30.{middle}.signature"


REQUIRED = (1 << 1) | (1 << 2) | (1 << 7)
READ_ONLY = 1 << 30


def test_accepts_read_only_with_required_categories() -> None:
    require_collection_token(token(scope=REQUIRED | READ_ONLY))


def test_accepts_writable_token_for_read_only_collection() -> None:
    require_collection_token(token(scope=REQUIRED))


@pytest.mark.parametrize(
    ("candidate", "message"),
    [
        (token(scope=READ_ONLY | (1 << 1) | (1 << 7)), "Аналитика"),
        (token(scope=READ_ONLY | REQUIRED, account=2, test=True), "Тестовый"),
        ("not-a-jwt", "JWT"),
        (token(scope=-1), "маски прав"),
    ],
)
def test_rejects_insufficient_or_unparseable_token(candidate: str, message: str) -> None:
    with pytest.raises(WbTokenAccessError, match=message):
        require_collection_token(candidate)


async def test_collect_accepts_writable_token_and_reaches_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DatabaseReachedError(Exception):
        pass

    def stop_at_database(_: str) -> None:
        raise DatabaseReachedError

    monkeypatch.setattr(jobs, "build_engine", stop_at_database)
    settings = load_settings()
    secrets = Secrets(
        database_url="postgresql+asyncpg://ignored",
        wb_api_token=token(scope=REQUIRED),
    )
    with pytest.raises(DatabaseReachedError):
        await jobs.collect(
            settings,
            secrets,
            SystemClock(settings.period.tz),
            dt.date(2026, 10, 13),
        )
