"""A6API must confirm live prices before a paid AI request."""

from decimal import Decimal

import httpx
import pytest
from app.config import ConfigError
from app.services.ai_cost import ModelTariff
from app.services.ai_pricing import verify_current_a6_tariff


def _transport(*, ratio: float = 0.5, currency: str = "USD") -> httpx.MockTransport:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/pricing":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "model_name": "deepseek-v4.1-flash",
                            "quota_type": 0,
                            "model_ratio": ratio,
                            "completion_ratio": 1,
                            "enable_groups": ["default"],
                        }
                    ],
                    "group_ratio": {"default": 1},
                    "usable_group": {"default": "Default"},
                },
            )
        if request.url.path == "/api/status":
            return httpx.Response(
                200,
                json={"data": {"quota_per_unit": 500000, "quota_display_type": currency}},
            )
        raise AssertionError(f"unexpected request: {request.url.path}")

    return httpx.MockTransport(respond)


async def test_live_a6_price_matches_configured_budget_tariff() -> None:
    tariff = ModelTariff("deepseek-v4.1-flash", Decimal("1"), Decimal("1"))

    await verify_current_a6_tariff(
        tariff,
        pricing_url="https://api.example.invalid/api/pricing",
        api_key="DemoRedacted",
        transport=_transport(),
    )


async def test_changed_live_price_blocks_paid_request() -> None:
    tariff = ModelTariff("deepseek-v4.1-flash", Decimal("1"), Decimal("1"))

    with pytest.raises(ConfigError, match="изменился"):
        await verify_current_a6_tariff(
            tariff,
            pricing_url="https://api.example.invalid/api/pricing",
            api_key="DemoRedacted",
            transport=_transport(ratio=0.75),
        )


async def test_unknown_currency_blocks_paid_request() -> None:
    tariff = ModelTariff("deepseek-v4.1-flash", Decimal("1"), Decimal("1"))

    with pytest.raises(ConfigError, match="подтвердить"):
        await verify_current_a6_tariff(
            tariff,
            pricing_url="https://api.example.invalid/api/pricing",
            api_key="DemoRedacted",
            transport=_transport(currency="CNY"),
        )


@pytest.mark.parametrize(
    "pricing_url",
    [
        "https://api.example.invalid:8443/api/pricing",
        "https://user:DemoEmail/api/pricing",
        "https://api.example.invalid/other/pricing",
        "https://api.example.invalid/api/pricing?redirect=1",
    ],
)
async def test_unexpected_pricing_endpoint_blocks_before_request(pricing_url: str) -> None:
    tariff = ModelTariff("deepseek-v4.1-flash", Decimal("1"), Decimal("1"))

    with pytest.raises(ConfigError, match="Источник тарифа"):
        await verify_current_a6_tariff(
            tariff,
            pricing_url=pricing_url,
            api_key="DemoRedacted",
            transport=_transport(),
        )
