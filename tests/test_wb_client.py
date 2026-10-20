"""Контрактные тесты клиента Wildberries на сохранённых фикстурах, без сети.

Фикстуры собраны по официальным спецификациям 24.09.2026. Если WB поменяет
контракт, эти тесты — первое место, где это станет видно.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx
import pytest
import respx
from app.adapters.wb.client import (
    ANALYTICS_HOST,
    CONTENT_SANDBOX_HOST,
    FEEDBACKS_SANDBOX_HOST,
    RateLimits,
    WbAuthError,
    WbClient,
    WbReadOnlyError,
)

FIXTURES = Path(__file__).parent / "fixtures" / "wb"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def client() -> WbClient:
    return WbClient("DemoRedactedCredential", sandbox=True, limits=RateLimits.unthrottled())


@respx.mock
async def test_client_refuses_unlisted_write_endpoint(client: WbClient) -> None:
    async with client:
        with pytest.raises(WbReadOnlyError, match="разрешённого чтения"):
            await client._request(
                "POST",
                f"{CONTENT_SANDBOX_HOST}/content/v2/cards/upload",
                client._content_limit,
                json={"cards": []},
            )
    assert not respx.calls


@respx.mock
async def test_category_requests_use_their_own_tokens() -> None:
    feedbacks = respx.get(f"{FEEDBACKS_SANDBOX_HOST}/ping").mock(
        return_value=httpx.Response(200, json={"Status": "OK"})
    )
    cards = respx.post(f"{CONTENT_SANDBOX_HOST}/content/v2/get/cards/list").mock(
        return_value=httpx.Response(200, json=load("cards_page1"))
    )
    analytics = respx.post(f"{ANALYTICS_HOST}/api/analytics/v2/item-rating").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    async with WbClient(
        "feedbacks-token",
        content_token="DemoRedacted",
        analytics_token="DemoRedacted",
        sandbox=True,
        limits=RateLimits.unthrottled(),
    ) as client:
        await client.ping()
        [card async for card in client.iter_cards()]
        await client._request(
            "POST",
            f"{ANALYTICS_HOST}/api/analytics/v2/item-rating",
            client._analytics_limit,
            json={},
        )

    assert feedbacks.calls[0].request.headers["Authorization"] == "feedbacks-token"
    assert cards.calls[0].request.headers["Authorization"] == "DemoRedacted"
    assert analytics.calls[0].request.headers["Authorization"] == "DemoRedacted"


@respx.mock
async def test_cards_pagination_stops_on_short_page(client: WbClient) -> None:
    """Страница короче лимита — значит последняя. Второго запроса быть не должно."""
    route = respx.post(f"{CONTENT_SANDBOX_HOST}/content/v2/get/cards/list").mock(
        return_value=httpx.Response(200, json=load("cards_page1"))
    )
    async with client:
        cards = [card async for card in client.iter_cards()]

    assert route.call_count == 1
    assert [c.nm_id for c in cards] == [4016747329, 3118800491]
    assert cards[0].vendor_code == "DemoPhone"
    assert cards[0].size == "M"
    assert cards[1].size is None, "у товара без размеров размер пустой, а не падение"


@respx.mock
async def test_cards_request_asks_for_all_cards(client: WbClient) -> None:
    """withPhoto=-1 обязателен: со значением по умолчанию половина каталога не придёт."""
    route = respx.post(f"{CONTENT_SANDBOX_HOST}/content/v2/get/cards/list").mock(
        return_value=httpx.Response(200, json=load("cards_page1"))
    )
    async with client:
        [card async for card in client.iter_cards()]

    payload = json.loads(route.calls[0].request.content)
    assert payload["settings"]["sort"]["ascending"] is True
    assert payload["settings"]["filter"]["withPhoto"] == -1
    assert payload["settings"]["cursor"]["limit"] == 100


@respx.mock
async def test_overlapping_card_pages_yield_each_product_once(client: WbClient) -> None:
    """WB может повторить границу страниц во время обновления каталога."""
    respx.post(f"{CONTENT_SANDBOX_HOST}/content/v2/get/cards/list").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "cards": [
                        {"nmID": 800_000_000 + number, "vendorCode": f"SKU-{number}"}
                        for number in range(100)
                    ],
                    "cursor": {
                        "updatedAt": "DemoPhoneT10:00:00Z",
                        "nmID": 800_000_099,
                        "total": 100,
                    },
                },
            ),
            httpx.Response(
                200,
                json={
                    "cards": [
                        {"nmID": 800_000_099, "vendorCode": "SKU-99"},
                        {"nmID": 800_000_100, "vendorCode": "SKU-100"},
                    ],
                    "cursor": {"total": 2},
                },
            ),
        ]
    )

    async with client:
        cards = [card async for card in client.iter_cards()]

    assert len(cards) == 101
    assert len({card.nm_id for card in cards}) == 101


@respx.mock
async def test_trash_cards_are_loaded_with_their_own_cursor(client: WbClient) -> None:
    """Отзывы могут относиться к карточкам в корзине, отсутствующим в обычном списке."""
    route = respx.post(f"{CONTENT_SANDBOX_HOST}/content/v2/get/cards/trash").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "cards": [
                        {"nmID": 900_000_000 + number, "vendorCode": f"ARCH-{number}"}
                        for number in range(100)
                    ],
                    "cursor": {
                        "trashedAt": "DemoPhoneT10:00:00Z",
                        "nmID": 900_000_099,
                        "total": 100,
                    },
                },
            ),
            httpx.Response(
                200,
                json={
                    "cards": [{"nmID": 900_000_100, "vendorCode": "ARCH-100"}],
                    "cursor": {"total": 1},
                },
            ),
        ]
    )

    async with client:
        cards = [card async for card in client.iter_trash_cards()]

    assert len(cards) == 101
    assert cards[-1].vendor_code == "ARCH-100"
    assert route.call_count == 2
    first = json.loads(route.calls[0].request.content)
    second = json.loads(route.calls[1].request.content)
    assert first["settings"]["sort"]["ascending"] is True
    assert first["settings"]["cursor"] == {"limit": 100}
    assert second["settings"]["cursor"] == {
        "limit": 100,
        "trashedAt": "DemoPhoneT10:00:00Z",
        "nmID": 900_000_099,
    }


@respx.mock
async def test_sales_funnel_requests_all_products_in_batch(client: WbClient) -> None:
    """WB отдаёт только 50 товаров, когда limit не указан."""
    ids = list(range(123_000_000, 123_000_060))

    def response(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        offset = payload.get("offset", 0)
        limit = payload.get("limit", 50)
        selected = payload["nmIds"][offset : offset + limit]
        return httpx.Response(
            200,
            json={"data": {"products": [{"product": {"nmId": nm_id}} for nm_id in selected]}},
        )

    respx.post(f"{ANALYTICS_HOST}/api/analytics/v3/sales-funnel/products").mock(
        side_effect=response
    )
    async with client:
        products = await client.fetch_sales_funnel(
            ids, period_start=dt.date(2026, 10, 7), period_end=dt.date(2026, 10, 13)
        )

    assert [item.product.nm_id for item in products] == ids


@respx.mock
async def test_feedbacks_makes_two_passes(client: WbClient) -> None:
    """isAnswered обязателен и «оба сразу» не умеет — нужны два прохода."""
    route = respx.get(f"{FEEDBACKS_SANDBOX_HOST}/api/v1/feedbacks").mock(
        return_value=httpx.Response(200, json=load("feedbacks_page"))
    )
    async with client:
        feedbacks = [
            fb
            async for fb in client.iter_feedbacks(date_from=dt.datetime(2026, 9, 17, tzinfo=dt.UTC))
        ]

    assert route.call_count == 2, "нужны проходы по обработанным и необработанным"
    answered = {call.request.url.params["isAnswered"] for call in route.calls}
    assert answered == {"false", "true"}
    assert len(feedbacks) == 2
    assert feedbacks[0].nm_id == 4016747329
    assert feedbacks[0].valuation == 2


@respx.mock
async def test_archive_stops_outside_period(client: WbClient) -> None:
    """У архива нет фильтра по дате: идём от свежих и останавливаемся сами."""
    respx.get(f"{FEEDBACKS_SANDBOX_HOST}/api/v1/feedbacks/archive").mock(
        return_value=httpx.Response(200, json=load("feedbacks_page"))
    )
    async with client:
        feedbacks = [
            fb
            async for fb in client.iter_archive_feedbacks(
                since=dt.datetime(2026, 9, 24, tzinfo=dt.UTC)
            )
        ]

    assert feedbacks == [], "отзыв от 23-го старше окна с 24-го — не берём"


@respx.mock
async def test_retry_on_429(client: WbClient) -> None:
    """429 — не ошибка, а просьба подождать. Повторяем."""
    route = respx.post(f"{CONTENT_SANDBOX_HOST}/content/v2/get/cards/list").mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json=load("cards_page1")),
        ]
    )
    async with client:
        cards = [card async for card in client.iter_cards()]

    assert route.call_count == 2
    assert len(cards) == 2


@respx.mock
async def test_no_retry_on_401(client: WbClient) -> None:
    """401 — токен отозван, повтор бессмыслен: падаем сразу и понятным текстом."""
    route = respx.post(f"{CONTENT_SANDBOX_HOST}/content/v2/get/cards/list").mock(
        return_value=httpx.Response(401)
    )
    async with client:
        with pytest.raises(WbAuthError, match="401"):
            [card async for card in client.iter_cards()]

    assert route.call_count == 1, "повторять запрос с мёртвым токеном незачем"
