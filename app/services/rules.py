"""Четыре правила сигналов из ТЗ п. 8. FR-3.1…FR-3.5, TSK-204.

Каждое правило — чистая функция: на входе приросты за период и базу,
на выходе сигнал или None. Базы данных они не знают, поэтому проверяются
табличными тестами без Postgres.

Пятое правило ТЗ (аномальная динамика через AI) намеренно не делается:
п. 8.5 сам говорит «в дальнейшем предусмотреть».

Общий принцип, ради которого всё затевалось (ТЗ п. 4): сравниваются доли
в притоке оценок, а не абсолютные количества, и каждое изменение доли
проверяется на значимость при фактическом размере выборки.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.repositories.signals import SignalRow, SignalSetting
from app.services.periods import PeriodCounts
from app.services.significance import share_shift

RATING_CHANGE = "rating_change"
NEGATIVE_SPIKE = "negative_spike"
TOP_RATING_DROP = "top_rating_drop"
BUYOUT_DROP = "buyout_drop"


def rating_change(
    current: PeriodCounts,
    base: PeriodCounts,
    setting: SignalSetting,
) -> SignalRow | None:
    """Рейтинг артикула изменился сверх порога. ТЗ п. 8.1, FR-3.1.

    Здесь z-тест не нужен: рейтинг — уже агрегат, посчитанный Wildberries,
    а не наша выборка.
    """
    before, after = current.rating_before, current.rating_after
    if before is None or after is None:
        return None
    delta = after - before
    if abs(delta) < setting.threshold:
        return None
    return SignalRow(
        nm_id=current.nm_id,
        signal_type=RATING_CHANGE,
        period_start=current.period.start,
        period_end=current.period.end,
        value_before=round(before, 2),
        value_after=round(after, 2),
        delta=round(delta, 4),
        payload={
            "rating_before": round(before, 2),
            "rating_after": round(after, 2),
            "threshold": setting.threshold,
            "new_marks": current.total,
        },
    )


def _share_signal(
    *,
    signal_type: str,
    current: PeriodCounts,
    base: PeriodCounts,
    setting: SignalSetting,
    pick: Callable[[PeriodCounts], int],
    direction: int,
    label: str,
) -> SignalRow | None:
    """Общая механика правил, сравнивающих долю категории.

    `direction` +1 — сигналим на рост доли, −1 — на падение.
    """
    shift = share_shift(
        k_now=pick(current),
        n_now=current.total,
        k_base=pick(base),
        n_base=base.total,
        cfg=setting.significance(),
    )
    if shift is None:
        return None
    if direction > 0 and shift.delta_pp <= 0:
        return None
    if direction < 0 and shift.delta_pp >= 0:
        return None
    return SignalRow(
        nm_id=current.nm_id,
        signal_type=signal_type,
        period_start=current.period.start,
        period_end=current.period.end,
        value_before=round(shift.share_base * 100, 2),
        value_after=round(shift.share_now * 100, 2),
        delta=round(shift.delta_pp, 2),
        payload={
            "metric": label,
            "share_base_pct": round(shift.share_base * 100, 2),
            "share_now_pct": round(shift.share_now * 100, 2),
            "delta_pp": round(shift.delta_pp, 2),
            "z": round(shift.z, 3),
            "marks_now": pick(current),
            "marks_total_now": current.total,
            "marks_base": pick(base),
            "marks_total_base": base.total,
            "base_period": str(base.period),
            "threshold_pp": setting.threshold,
            "min_sample_size": setting.min_sample_size,
            "alpha": setting.alpha,
        },
    )


def negative_spike(
    current: PeriodCounts,
    base: PeriodCounts,
    setting: SignalSetting,
) -> SignalRow | None:
    """Выросла доля оценок 1, 2 и 3. ТЗ п. 8.2, FR-3.2.

    AI-резюме причин прикладывается на этапе 4; до тех пор `ai_summary`
    остаётся пустым, и сигнал уходит без объяснения причины.
    """
    return _share_signal(
        signal_type=NEGATIVE_SPIKE,
        current=current,
        base=base,
        setting=setting,
        pick=lambda counts: counts.negative,
        direction=+1,
        label="доля оценок 1-3",
    )


def top_rating_drop(
    current: PeriodCounts,
    base: PeriodCounts,
    setting: SignalSetting,
) -> SignalRow | None:
    """Снизилась доля оценок 5. ТЗ п. 8.3, FR-3.3."""
    return _share_signal(
        signal_type=TOP_RATING_DROP,
        current=current,
        base=base,
        setting=setting,
        pick=lambda counts: counts.five,
        direction=-1,
        label="доля оценок 5",
    )


def buyout_drop(
    current: PeriodCounts,
    base: PeriodCounts,
    setting: SignalSetting,
) -> SignalRow | None:
    """Процент выкупа упал на порог и больше. ТЗ п. 8.4, FR-3.4.

    Единственный порог, заданный в ТЗ числом: 0,2 процентного пункта.
    Минимальная выборка по заказам обязательна — на 30 заказах один
    невыкуп сдвигает цифру сразу на 3,3 п.п.
    """
    before, after = current.buyout_before, current.buyout_after
    if before is None or after is None:
        return None
    if current.orders is not None and current.orders < setting.min_sample_size:
        return None
    delta = after - before
    if delta > -setting.threshold:
        return None
    return SignalRow(
        nm_id=current.nm_id,
        signal_type=BUYOUT_DROP,
        period_start=current.period.start,
        period_end=current.period.end,
        value_before=round(before, 2),
        value_after=round(after, 2),
        delta=round(delta, 2),
        payload={
            "buyout_before_pct": round(before, 2),
            "buyout_after_pct": round(after, 2),
            "delta_pp": round(delta, 2),
            "orders": current.orders,
            "threshold_pp": setting.threshold,
            "min_orders": setting.min_sample_size,
        },
    )


Rule = Callable[[PeriodCounts, PeriodCounts, SignalSetting], SignalRow | None]

ALL_RULES: dict[str, Rule] = {
    RATING_CHANGE: rating_change,
    NEGATIVE_SPIKE: negative_spike,
    TOP_RATING_DROP: top_rating_drop,
    BUYOUT_DROP: buyout_drop,
}


def describe(row: SignalRow) -> dict[str, Any]:
    """Короткое представление для логов."""
    return {"nm_id": row.nm_id, "type": row.signal_type, "delta": row.delta}
