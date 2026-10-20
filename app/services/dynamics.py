"""Расчёт изменений между снимками — FR-2.1, FR-2.3.

Дельты не хранятся, а считаются в момент запроса (FR-2.2): правка формулы
не должна требовать миграции истории.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

NEW = "новое"
"""Было 0, стало больше нуля. Не «+∞» и не «+100%» — FR-2.3."""

NO_DATA = "-"
"""Было 0 и стало 0."""


@dataclass(frozen=True, slots=True)
class Delta:
    """Изменение счётчика между периодами."""

    before: int
    after: int
    percent: float | None
    """None, когда процент не определён: смотри `label`."""
    label: str
    """Готовая для отчёта строка: «+148,0%», «-100,0%», «новое», «-»."""


def count_delta(before: int, after: int) -> Delta:
    """Изменение количества оценок с правилами для нулей.

    Таблица из docs/02_ГЭПЫ_И_ВОПРОСЫ.md → G-12:
        0 → N   «новое»
        N → 0   «-100,0%»
        0 → 0   «-»
        N → M   (M/N - 1) × 100%
    """
    if before == 0 and after == 0:
        return Delta(before, after, None, NO_DATA)
    if before == 0:
        return Delta(before, after, None, NEW)
    percent = (after / before - 1) * 100
    return Delta(before, after, percent, format_percent(percent))


def format_percent(value: float) -> str:
    """Проценты в формате ТЗ п. 3: со знаком, один знак после запятой, запятая."""
    return f"{value:+.1f}%".replace(".", ",")


Category = Literal[5, 4, 3, 2, 1]


def share(k: int, n: int) -> float | None:
    """Доля категории в приросте оценок за период. None — оценок не было."""
    return k / n if n > 0 else None
