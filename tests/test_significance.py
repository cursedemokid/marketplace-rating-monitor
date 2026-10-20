"""Контрольные кейсы значимости — прямо из ТЗ и tasks.md TSK-203.

Эти тесты написаны до кода и меняться не должны: они фиксируют то, как
заказчик просил считать негативную динамику (ТЗ п. 4).
"""

from __future__ import annotations

import pytest
from app.services.significance import SignificanceConfig, share_shift

CFG = SignificanceConfig(min_delta_pp=5.0, min_sample_size=10, alpha=0.01)


def test_tz_example_not_a_negative_signal() -> None:
    """Пример из ТЗ п. 4: 100 из 200 → 50 из 50.

    Доля пятёрок выросла с 50% до 100%. Это РОСТ, а не падение —
    сигнала о негативе быть не должно.
    """
    shift = share_shift(k_now=50, n_now=50, k_base=100, n_base=200, cfg=CFG)
    assert shift is not None
    assert shift.delta_pp > 0, "доля пятёрок выросла, это не негативная динамика"
    assert shift.share_base == pytest.approx(0.5)
    assert shift.share_now == pytest.approx(1.0)


def test_two_bad_of_three_is_noise() -> None:
    """2 плохих отзыва из 3 — не событие: выборка слишком мала."""
    assert share_shift(k_now=2, n_now=3, k_base=10, n_base=200, cfg=CFG) is None


def test_real_negative_spike() -> None:
    """40 негативных из 200 против вчерашних 10 из 200 — значимо."""
    shift = share_shift(k_now=40, n_now=200, k_base=10, n_base=200, cfg=CFG)
    assert shift is not None
    assert shift.delta_pp == pytest.approx(15.0)
    assert shift.z > 0


def test_small_delta_below_threshold() -> None:
    """Изменение доли на 2 п.п. ниже порога в 5 п.п. — молчим."""
    assert share_shift(k_now=24, n_now=200, k_base=20, n_base=200, cfg=CFG) is None


def test_empty_period() -> None:
    """За период не пришло ни одной оценки — делить не на что."""
    assert share_shift(k_now=0, n_now=0, k_base=10, n_base=100, cfg=CFG) is None
