"""Реализации каналов доставки. TSK-301, TSK-302.

Яндекс Мессенджер — основной, Telegram — запасной. Почта (SMTP) в первой
версии не сделана: FR-4.2 требует «не менее двух каналов», и они есть,
а заводить зависимость ради канала, которого никто не просил, рано.
"""

from __future__ import annotations

import httpx
from loguru import logger

from app.adapters.messenger.base import DeliveryResult
from app.adapters.messenger.yandex import YandexMessengerBot

TELEGRAM_API = "https://api.telegram.org"


class YandexMessengerChannel:
    """Бот Яндекс 360, отправляющий сигналы в чат Телемоста (G-01)."""

    name = "yandex_messenger"

    def __init__(self, bot: YandexMessengerBot, chat_id: str) -> None:
        self._bot = bot
        self._chat_id = chat_id

    async def send(self, text: str, *, payload_id: str = "") -> DeliveryResult:
        try:
            message_id = await self._bot.send_text(
                self._chat_id, text, payload_id=payload_id or text[:64]
            )
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logger.warning("Яндекс Мессенджер не принял сообщение: {}", exc)
            return DeliveryResult(ok=False, error=str(exc))
        return DeliveryResult(ok=True, message_id=message_id)


class TelegramChannel:
    """Запасной канал: если Яндекс 360 у заказчика нет (G-01)."""

    name = "telegram"

    def __init__(self, token: str, chat_id: str, *, timeout: float = 15.0) -> None:
        self._client = httpx.AsyncClient(base_url=f"{TELEGRAM_API}/bot{token}", timeout=timeout)
        self._chat_id = chat_id

    async def send(self, text: str, *, payload_id: str = "") -> DeliveryResult:
        try:
            response = await self._client.post(
                "/sendMessage",
                json={"chat_id": self._chat_id, "text": text},
            )
            response.raise_for_status()
            message_id = int(response.json()["result"]["message_id"])
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logger.warning("Telegram не принял сообщение: {}", exc)
            return DeliveryResult(ok=False, error=str(exc))
        return DeliveryResult(ok=True, message_id=message_id)

    async def aclose(self) -> None:
        await self._client.aclose()
