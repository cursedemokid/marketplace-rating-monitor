"""Проверка текущего тарифа A6API перед платными запросами проекта."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import httpx

from app.config import ConfigError
from app.services.ai_cost import ModelTariff


def _positive_decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("invalid pricing number")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid pricing number") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError("invalid pricing number")
    return number


def _live_tariff(pricing: object, status: object, model: str) -> ModelTariff:
    if not isinstance(pricing, dict) or not isinstance(status, dict):
        raise ValueError("invalid A6API response")
    rows = pricing.get("data")
    groups = pricing.get("usable_group")
    ratios = pricing.get("group_ratio")
    status_data = status.get("data")
    if (
        pricing.get("success") is not True
        or not isinstance(rows, list)
        or not isinstance(groups, dict)
        or len(groups) != 1
        or not isinstance(ratios, dict)
        or not isinstance(status_data, dict)
        or status_data.get("quota_display_type") != "USD"
    ):
        raise ValueError("unknown A6API billing configuration")
    group = next(iter(groups))
    matches = [row for row in rows if isinstance(row, dict) and row.get("model_name") == model]
    if len(matches) != 1:
        raise ValueError("model pricing missing or ambiguous")
    row = matches[0]
    if row.get("quota_type") != 0 or group not in row.get("enable_groups", []):
        raise ValueError("unsupported A6API billing mode or group")
    per_unit = _positive_decimal(status_data.get("quota_per_unit"))
    model_ratio = _positive_decimal(row.get("model_ratio"))
    completion_ratio = _positive_decimal(row.get("completion_ratio"))
    group_ratio = _positive_decimal(ratios.get(group))
    input_price = Decimal(1_000_000) * model_ratio * group_ratio / per_unit
    return ModelTariff(model, input_price, input_price * completion_ratio)


async def verify_current_a6_tariff(
    tariff: ModelTariff,
    *,
    pricing_url: str,
    api_key: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Остановить вызов, если A6API изменил цену или не отдал параметры счёта."""
    parsed = urlsplit(pricing_url)
    if (
        parsed.scheme != "https"
        or parsed.netloc not in {"api.example.invalid", "api.example.invalid"}
        or parsed.path != "/api/pricing"
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError("Источник тарифа должен вести на HTTPS A6API")
    status_url = f"{parsed.scheme}://{parsed.netloc}/api/status"
    try:
        async with httpx.AsyncClient(timeout=20, transport=transport) as client:
            headers = {"Authorization": f"Bearer {api_key}"}
            pricing_response = await client.get(pricing_url, headers=headers)
            pricing_response.raise_for_status()
            status_response = await client.get(status_url, headers=headers)
            status_response.raise_for_status()
            live = _live_tariff(pricing_response.json(), status_response.json(), tariff.model)
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise ConfigError("Не удалось подтвердить текущий тариф A6API; AI-вызов отменён") from exc
    if (
        live.input_usd_per_1m != tariff.input_usd_per_1m
        or live.output_usd_per_1m != tariff.output_usd_per_1m
    ):
        raise ConfigError("Тариф A6API изменился; обновите цены в config/settings.yaml")
