"""Тексты сигналов. TSK-303, ТЗ п. 5 и п. 8.

Образец из ТЗ:

    Арт. DemoPhone: зафиксирован всплеск негативных отзывов.
    Основная причина — жалобы на протечки.

Отсюда три правила: обращение по артикулу продавца (не по nmId — человек
знает товар по своему артикулу), одна фраза о сути, причина следом.
Цифры дают второй строкой: без них сотрудник пойдёт перепроверять руками.

Тексты читает человек в чате, поэтому проценты по-русски — с запятой.
"""

from __future__ import annotations

from app.repositories.signals import PendingSignal
from app.services import rules
from app.services.dynamics import format_percent

MAX_LENGTH = 6000
"""Лимит Bot API Яндекса. Подробности не влезли — уводим в выгрузку."""


def _pp(value: float | None) -> str:
    """Процентные пункты со знаком: «+15,0 п.п.»."""
    return "—" if value is None else f"{format_percent(value)[:-1]} п.п."


def _share(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}".replace(".", ",") + "%"


def render(signal: PendingSignal) -> str:
    """Текст одного сигнала."""
    head = f"Арт. {signal.vendor_code}"
    if signal.signal_type == rules.NEGATIVE_SPIKE:
        lines = [
            f"{head}: зафиксирован всплеск негативных отзывов.",
            f"Доля оценок 1–3: {_share(signal.value_before)} → {_share(signal.value_after)} "
            f"({_pp(signal.delta)}).",
        ]
        marks = signal.payload.get("marks_now")
        total = signal.payload.get("marks_total_now")
        if marks is not None and total is not None:
            lines.append(f"За период: {marks} негативных из {total} оценок.")
        if signal.ai_summary:
            lines.append(f"Основная причина — {signal.ai_summary}.")
    elif signal.signal_type == rules.TOP_RATING_DROP:
        lines = [
            f"{head}: снизилась доля оценок 5.",
            f"Доля пятёрок: {_share(signal.value_before)} → {_share(signal.value_after)} "
            f"({_pp(signal.delta)}).",
        ]
    elif signal.signal_type == rules.RATING_CHANGE:
        direction = "снизился" if (signal.delta or 0) < 0 else "вырос"
        lines = [
            f"{head}: рейтинг {direction}.",
            f"Рейтинг: {signal.value_before} → {signal.value_after}.",
        ]
    elif signal.signal_type == rules.BUYOUT_DROP:
        lines = [
            f"{head}: снизился процент выкупа.",
            f"Выкуп: {_share(signal.value_before)} → {_share(signal.value_after)} "
            f"({_pp(signal.delta)}).",
        ]
        orders = signal.payload.get("orders")
        if orders is not None:
            lines.append(f"Заказов за период: {orders}.")
    else:
        # Неизвестный тип лучше отправить сырым, чем проглотить: молчание
        # системы мониторинга хуже некрасивого сообщения.
        lines = [f"{head}: {signal.signal_type}, изменение {signal.delta}."]

    lines.append(f"Период: {signal.period_start:%d.%m.%Y}—{signal.period_end:%d.%m.%Y}.")
    text = "\n".join(lines)
    return text[:MAX_LENGTH]
