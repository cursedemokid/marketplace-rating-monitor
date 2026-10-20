"""Табличные тесты четырёх правил сигналов. TSK-204, AC-3.

Контрольные случаи взяты из ТЗ и docs/02 и меняться не должны: они
фиксируют то, как заказчик просил считать негативную динамику.
"""

from __future__ import annotations

import datetime as dt

import pytest
from app.repositories.signals import SignalSetting
from app.services.periods import Period, PeriodCounts
from app.services.rules import buyout_drop, negative_spike, rating_change, top_rating_drop

DAY = Period(dt.date(2026, 9, 24), dt.date(2026, 9, 24))
BASE = Period(dt.date(2026, 9, 10), dt.date(2026, 9, 23))


def counts(
    *,
    period: Period,
    total: int = 0,
    five: int = 0,
    four: int = 0,
    three: int = 0,
    two: int = 0,
    one: int = 0,
    rating_before: float | None = None,
    rating_after: float | None = None,
    buyout_before: float | None = None,
    buyout_after: float | None = None,
    orders: int | None = None,
) -> PeriodCounts:
    return PeriodCounts(
        nm_id=1,
        period=period,
        total=total,
        five=five,
        four=four,
        three=three,
        two=two,
        one=one,
        rating_before=rating_before,
        rating_after=rating_after,
        buyout_before=buyout_before,
        buyout_after=buyout_after,
        orders=orders,
    )


def setting(
    signal_type: str,
    *,
    threshold: float,
    min_sample_size: int = 10,
    alpha: float = 0.01,
) -> SignalSetting:
    return SignalSetting(
        signal_type=signal_type,
        threshold=threshold,
        min_sample_size=min_sample_size,
        alpha=alpha,
        suppress_hours=24,
        baseline_days=14,
        is_enabled=True,
    )


# --- изменение рейтинга -----------------------------------------------------


def test_rating_change_above_threshold() -> None:
    current = counts(period=DAY, total=20, rating_before=4.93, rating_after=4.91)
    signal = rating_change(current, counts(period=BASE), setting("rating_change", threshold=0.01))
    assert signal is not None
    assert signal.delta == pytest.approx(-0.02)


def test_rating_change_below_threshold_is_silent() -> None:
    """Рейтинг дёргается на сотые каждый день — это не повод будить человека."""
    current = counts(period=DAY, total=20, rating_before=4.930, rating_after=4.935)
    assert (
        rating_change(current, counts(period=BASE), setting("rating_change", threshold=0.01))
        is None
    )


def test_rating_change_without_snapshot_is_silent() -> None:
    current = counts(period=DAY, total=20, rating_before=None, rating_after=4.9)
    assert (
        rating_change(current, counts(period=BASE), setting("rating_change", threshold=0.01))
        is None
    )


# --- всплеск негатива -------------------------------------------------------


def test_negative_spike_is_detected() -> None:
    """40 негативных из 200 против 10 из 200 в базе — значимый рост."""
    current = counts(period=DAY, total=200, one=20, two=10, three=10)
    base = counts(period=BASE, total=200, one=4, two=3, three=3)
    signal = negative_spike(current, base, setting("negative_spike", threshold=5.0))
    assert signal is not None
    assert signal.delta == pytest.approx(15.0)
    assert signal.payload["marks_now"] == 40
    assert signal.payload["z"] > 0


def test_two_bad_of_three_is_noise() -> None:
    """2 плохих отзыва из 3 сигнал не поднимают — выборка мала. AC-3."""
    current = counts(period=DAY, total=3, one=2)
    base = counts(period=BASE, total=200, one=4, two=3, three=3)
    assert negative_spike(current, base, setting("negative_spike", threshold=5.0)) is None


def test_negative_share_falling_is_not_a_spike() -> None:
    """Доля негатива упала — это хорошая новость, а не сигнал о всплеске."""
    current = counts(period=DAY, total=200, one=2, two=2, three=2)
    base = counts(period=BASE, total=200, one=20, two=10, three=10)
    assert negative_spike(current, base, setting("negative_spike", threshold=5.0)) is None


# --- падение доли пятёрок ---------------------------------------------------


def test_tz_example_does_not_fire() -> None:
    """Контрольный пример ТЗ п. 4: 100 из 200 → 50 из 50.

    Пятёрок стало вдвое меньше в штуках, но их доля выросла с 50% до 100%.
    Сигнала о падении быть не должно — ровно это заказчик и просил.
    """
    current = counts(period=DAY, total=50, five=50)
    base = counts(period=BASE, total=200, five=100)
    assert top_rating_drop(current, base, setting("top_rating_drop", threshold=5.0)) is None


def test_top_rating_drop_is_detected() -> None:
    current = counts(period=DAY, total=200, five=120, four=40, three=20, two=10, one=10)
    base = counts(period=BASE, total=200, five=180, four=20)
    signal = top_rating_drop(current, base, setting("top_rating_drop", threshold=5.0))
    assert signal is not None
    assert signal.delta == pytest.approx(-30.0)
    assert signal.value_before == pytest.approx(90.0)
    assert signal.value_after == pytest.approx(60.0)


# --- падение выкупа ---------------------------------------------------------


def test_buyout_drop_at_exactly_two_tenths() -> None:
    """Порог из ТЗ: снижение на 0,2 п.п. и более. Ровно 0,2 — уже сигнал."""
    current = counts(period=DAY, buyout_before=87.0, buyout_after=86.8, orders=500)
    signal = buyout_drop(
        current, counts(period=BASE), setting("buyout_drop", threshold=0.2, min_sample_size=30)
    )
    assert signal is not None
    assert signal.delta == pytest.approx(-0.2)


def test_buyout_drop_below_threshold() -> None:
    current = counts(period=DAY, buyout_before=87.0, buyout_after=86.9, orders=500)
    assert (
        buyout_drop(
            current, counts(period=BASE), setting("buyout_drop", threshold=0.2, min_sample_size=30)
        )
        is None
    )


def test_buyout_growth_is_not_a_signal() -> None:
    current = counts(period=DAY, buyout_before=86.0, buyout_after=88.0, orders=500)
    assert (
        buyout_drop(
            current, counts(period=BASE), setting("buyout_drop", threshold=0.2, min_sample_size=30)
        )
        is None
    )


def test_buyout_drop_needs_enough_orders() -> None:
    """На десятке заказов один невыкуп — это 10 п.п., и это ни о чём не говорит."""
    current = counts(period=DAY, buyout_before=90.0, buyout_after=80.0, orders=10)
    assert (
        buyout_drop(
            current, counts(period=BASE), setting("buyout_drop", threshold=0.2, min_sample_size=30)
        )
        is None
    )
