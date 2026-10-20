"""Правила нулей из docs/02_ГЭПЫ_И_ВОПРОСЫ.md → G-12 и tasks.md TSK-201."""

from __future__ import annotations

import pytest
from app.services.dynamics import NEW, NO_DATA, count_delta, format_percent


def test_zero_to_positive_is_new() -> None:
    assert count_delta(0, 5).label == NEW


def test_positive_to_zero_is_minus_hundred() -> None:
    assert count_delta(5, 0).label == "-100,0%"


def test_zero_to_zero_is_dash() -> None:
    assert count_delta(0, 0).label == NO_DATA


def test_regular_change() -> None:
    delta = count_delta(100, 248)
    assert delta.percent == pytest.approx(148.0)
    assert delta.label == "+148,0%"


def test_percent_format_matches_tz() -> None:
    """ТЗ п. 3 пишет проценты со знаком и запятой: «оценка 5: +148%»."""
    assert format_percent(-87.0) == "-87,0%"
    assert format_percent(0.0) == "+0,0%"
