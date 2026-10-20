"""Локальная проверка категорий токена WB до первого запроса к API."""

from __future__ import annotations

import base64
import binascii
import json

from app.config import ConfigError


class WbTokenAccessError(ConfigError):
    """Токен не подходит для сбора данных WB."""


_REQUIRED_CATEGORIES = {
    "Контент": 1,
    "Аналитика": 2,
    "Вопросы и отзывы": 7,
}


def token_claims(token: str) -> dict[str, object]:
    """Прочитать общедоступные поля JWT без вывода самого токена в диагностику.

    Уровень «чтение/запись» допустим: методы записи блокирует WbClient.
    Это локальная предварительная проверка; подпись и отзыв проверяет WB API.
    """
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise WbTokenAccessError("WB_API_TOKEN должен быть JWT из трёх частей")
    try:
        encoded = parts[1]
        payload = json.loads(
            base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        )
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise WbTokenAccessError("Не удалось прочитать JWT payload токена WB") from exc
    if not isinstance(payload, dict) or type(payload.get("s")) is not int or payload["s"] < 0:
        raise WbTokenAccessError("В JWT токена WB нет числовой маски прав s")
    if payload.get("t") is True or payload.get("acc") == 2:
        raise WbTokenAccessError(
            "Тестовый токен WB доступен только в песочнице; "
            "для сбора реальных данных нужен рабочий токен"
        )
    return payload


def require_categories(token: str, categories: tuple[str, ...]) -> dict[str, object]:
    """Проверить категории для конкретного метода API; подпись проверит WB."""
    payload = token_claims(token)
    scope = payload["s"]
    assert isinstance(scope, int)
    missing = [name for name in categories if not scope & (1 << _REQUIRED_CATEGORIES[name])]
    if missing:
        raise WbTokenAccessError("В токене WB не хватает категорий: " + ", ".join(missing))
    return payload


def require_collection_token(token: str) -> None:
    """Проверить единый токен для всех трёх категорий сбора."""
    require_categories(token, tuple(_REQUIRED_CATEGORIES))
