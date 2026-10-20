"""Канал доставки сигналов как протокол. FR-4.2, TSK-301.

Основной канал — бот чатов Телемоста для бизнеса через Bot API Яндекс 360.
Пока заказчик не подтвердил организацию и не выдал токен бота с ID чата,
оставляем переключение на Telegram. Почтовый канал пока не реализован.

Выбор канала — ключом `notify.channel` в config/settings.yaml, без правки
кода.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    ok: bool
    message_id: int | None = None
    error: str | None = None


class NotificationChannel(Protocol):
    """Куда уходит текст сигнала."""

    name: str

    async def send(self, text: str, *, payload_id: str = "") -> DeliveryResult:
        """Доставить сообщение. Исключения не выпускает: сбой — это `ok=False`.

        Причина: недоставленный сигнал не должен ронять прогон. Он остаётся
        в базе неотправленным и уйдёт при следующей попытке (FR-4.3).

        `payload_id` — ключ идемпотентности: у Bot API Яндекса он штатный,
        и повторная отправка того же сигнала не задваивает сообщение в чате.
        Каналы, которые такого не умеют, его просто игнорируют.
        """
        ...
