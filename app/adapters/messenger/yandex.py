"""Бот чатов Телемоста для бизнеса — канал доставки сигналов (FR-4.1, G-01).

Официальных библиотек Bot API не существует ни для одного языка, поэтому
клиент свой. Проверено по документации: https://yandex.ru/dev/messenger/doc/ru/

Бот создаётся в кабинете организации Яндекс 360, в разделе «Телемост → Боты».
Bot API использует историческое имя хоста messenger.yandex.net.
"""

from __future__ import annotations

from types import TracebackType
from typing import Self

import httpx

BASE_URL = "https://botapi.messenger.yandex.net/bot/v1"
MAX_TEXT_LENGTH = 6000


class YandexMessengerBot:
    """Отправка сигналов в чат. Бот пишет только туда, где он участник."""

    def __init__(self, oauth_token: str, *, timeout: float = 15.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"OAuth {oauth_token}"},
            timeout=timeout,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._client.aclose()

    async def send_text(self, chat_id: str, text: str, *, payload_id: str) -> int:
        """Текстовое сообщение. Возвращает message_id.

        payload_id — штатный ключ идемпотентности Bot API. Передаём туда
        идентификатор сигнала: повторный прогон notify физически не сможет
        продублировать сообщение в чате.
        """
        if len(text) > MAX_TEXT_LENGTH:
            msg = f"Текст длиннее {MAX_TEXT_LENGTH} символов, подробности отправляйте файлом"
            raise ValueError(msg)
        response = await self._client.post(
            "/messages/sendText/",
            json={"chat_id": chat_id, "text": text, "payload_id": payload_id},
        )
        response.raise_for_status()
        return int(response.json()["message_id"])

    async def send_file(self, chat_id: str, filename: str, blob: bytes) -> int:
        """Файл выгрузки. Здесь multipart, а не JSON — это отличие от sendText."""
        response = await self._client.post(
            "/messages/sendFile/",
            data={"chat_id": chat_id},
            files={"document": (filename, blob)},
        )
        response.raise_for_status()
        return int(response.json()["message_id"])
