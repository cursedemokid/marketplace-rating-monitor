"""Токены разных кабинетов и категорий не смешиваются."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from app import jobs
from app.config import ConfigError, Secrets


def token(scope: int, seller: str) -> str:
    payload = {"s": scope, "sid": seller, "acc": 3, "t": False}
    middle = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"e30.{middle}.signature"


FEEDBACKS = 1 << 7
CONTENT = 1 << 1
ANALYTICS = 1 << 2


def test_two_cabinets_use_their_own_category_tokens() -> None:
    default_feedbacks = token(FEEDBACKS, "seller-default")
    default_content = token(CONTENT | ANALYTICS, "seller-default")
    DemoSeller_feedbacks = token(FEEDBACKS | ANALYTICS, "seller-DemoSeller")
    DemoSeller_content = token(CONTENT, "seller-DemoSeller")
    secrets = Secrets(
        _env_file=None,
        database_url="postgresql+asyncpg://ignored",
        WbFeedbacksToken=default_feedbacks,
        WbContentToken=default_content,
        WbFeedbacksToken_DemoSeller=DemoSeller_feedbacks,
        WbContentToken_DemoSeller=DemoSeller_content,
    )

    profiles = jobs.resolve_wb_profiles(secrets)

    assert [(profile.key, profile.seller_id) for profile in profiles] == [
        ("default", "seller-default"),
        ("DemoSeller", "seller-DemoSeller"),
    ]
    assert profiles[0].analytics_token == default_content
    assert profiles[1].analytics_token == DemoSeller_feedbacks


def test_mismatched_seller_tokens_are_rejected() -> None:
    secrets = Secrets(
        _env_file=None,
        database_url="postgresql+asyncpg://ignored",
        WbFeedbacksToken=token(FEEDBACKS | ANALYTICS, "seller-a"),
        WbContentToken=token(CONTENT, "seller-b"),
    )

    with pytest.raises(ConfigError, match="разных кабинетов"):
        jobs.resolve_wb_profiles(secrets)


def test_missing_analytics_allows_review_collection() -> None:
    secrets = Secrets(
        _env_file=None,
        database_url="postgresql+asyncpg://ignored",
        WbFeedbacksToken=token(FEEDBACKS, "seller-a"),
        WbContentToken=token(CONTENT, "seller-a"),
        WbFeedbacksToken_DemoSeller=token(FEEDBACKS, "seller-b"),
        WbContentToken_DemoSeller=token(CONTENT, "seller-b"),
    )

    profiles = jobs.resolve_wb_profiles(secrets)

    assert profiles[0].analytics_token is None
    assert profiles[1].analytics_token is None


def test_legacy_single_token_still_works() -> None:
    combined = token(FEEDBACKS | CONTENT | ANALYTICS, "seller-a")
    secrets = Secrets(
        _env_file=None,
        database_url="postgresql+asyncpg://ignored",
        wb_api_token=combined,
    )

    profiles = jobs.resolve_wb_profiles(secrets)

    assert len(profiles) == 1
    assert profiles[0].key == "default"
    assert profiles[0].feedbacks_token == combined
    assert profiles[0].content_token == combined
    assert profiles[0].analytics_token == combined


def test_two_profiles_cannot_point_to_same_seller() -> None:
    secrets = Secrets(
        _env_file=None,
        database_url="postgresql+asyncpg://ignored",
        WbFeedbacksToken=token(FEEDBACKS | ANALYTICS, "seller-a"),
        WbContentToken=token(CONTENT, "seller-a"),
        WbFeedbacksToken_DemoSeller=token(FEEDBACKS | ANALYTICS, "seller-a"),
        WbContentToken_DemoSeller=token(CONTENT, "seller-a"),
    )

    with pytest.raises(ConfigError, match="одинаковый sid"):
        jobs.resolve_wb_profiles(secrets)


def test_split_configuration_requires_both_cabinets() -> None:
    secrets = Secrets(
        _env_file=None,
        database_url="postgresql+asyncpg://ignored",
        WbFeedbacksToken=token(FEEDBACKS | ANALYTICS, "seller-a"),
        WbContentToken=token(CONTENT, "seller-a"),
    )

    with pytest.raises(ConfigError, match="default и DemoSeller"):
        jobs.resolve_wb_profiles(secrets)


def test_exact_env_variable_names_are_loaded(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql+asyncpg://ignored\n"
        "WbFeedbacksToken=feedbacks\n"
        "WbContentToken=content\n"
        "WbFeedbacksToken_DemoSeller=DemoSeller-feedbacks\n"
        "WbContentToken_DemoSeller=DemoSeller-content\n",
        encoding="utf-8",
    )

    secrets = Secrets(_env_file=env_file)

    assert secrets.wb_feedbacks_token == "feedbacks"
    assert secrets.wb_content_token == "content"
    assert secrets.wb_feedbacks_token_DemoSeller == "DemoSeller-feedbacks"
    assert secrets.wb_content_token_DemoSeller == "DemoSeller-content"
