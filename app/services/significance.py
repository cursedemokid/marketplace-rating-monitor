"""Проверка значимости изменения доли — FR-3.5, design.md § D5.

Порог в процентных пунктах сам по себе бесполезен: на трёх отзывах любое
значение его превышает. Поэтому сигнал уходит только при одновременном
выполнении трёх условий — практическая значимость, размер выборки и
статистическая значимость по двухпропорционному z-тесту.

Без этого система шлёт сигнал по каждому товару, где за день пришёл один
плохой отзыв, сотрудники отключают уведомления, и проект умирает
(docs/02_ГЭПЫ_И_ВОПРОСЫ.md → G-06).
"""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt

from scipy.stats import norm


@dataclass(frozen=True, slots=True)
class SignificanceConfig:
    """Пороги из config/settings.yaml. В коде констант нет."""

    min_delta_pp: float
    """Минимальное изменение доли в процентных пунктах."""
    min_sample_size: int
    """Минимум оценок за текущий период, иначе разговора нет."""
    alpha: float
    """Уровень значимости: 0.01 — различие с вероятностью ошибки 1%."""


@dataclass(frozen=True, slots=True)
class Shift:
    """Значимый сдвиг доли."""

    delta_pp: float
    z: float
    share_base: float
    share_now: float
    n_now: int


def share_shift(
    k_now: int,
    n_now: int,
    k_base: int,
    n_base: int,
    cfg: SignificanceConfig,
) -> Shift | None:
    """Сдвиг доли категории оценок между периодами. None — сигнала нет.

    k_* — сколько оценок этой категории пришло за период,
    n_* — сколько всего оценок пришло за период.

    Считаем именно по приросту между снимками, а не по накопленному итогу:
    накопленная доля почти не двигается, и сигналов не будет никогда
    (tasks.md → TSK-201, «частая ошибка»).
    """
    if n_now < cfg.min_sample_size or n_now <= 0 or n_base <= 0:
        return None

    share_base = k_base / n_base
    share_now = k_now / n_now
    delta_pp = (share_now - share_base) * 100
    if abs(delta_pp) < cfg.min_delta_pp:
        return None

    pooled = (k_base + k_now) / (n_base + n_now)
    if pooled in (0.0, 1.0):
        # Обе выборки целиком из одной категории — дисперсия нулевая,
        # z не определён. Различия долей нет по построению.
        return None

    se = sqrt(pooled * (1 - pooled) * (1 / n_base + 1 / n_now))
    z = (share_now - share_base) / se
    if abs(z) < float(norm.ppf(1 - cfg.alpha / 2)):
        return None

    return Shift(
        delta_pp=delta_pp,
        z=z,
        share_base=share_base,
        share_now=share_now,
        n_now=n_now,
    )
