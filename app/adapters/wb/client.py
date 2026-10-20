"""Транспорт к API Wildberries: авторизация, пейсинг, ретраи, пагинация.

Лимиты проверены по официальным спецификациям 24.09.2026:

| Категория            | Лимит                  | Особенность                       |
|----------------------|------------------------|-----------------------------------|
| Вопросы и отзывы     | 3 запроса/с            | общий на ВСЕ методы категории     |
| Контент              | 100 запросов/мин       | курсорная пагинация, до 100 карточек |
| Аналитика            | 3 запроса/мин          | item-rating — не более 50 nmId    |

Модели ответов — в `models.py`, и там же объяснено, почему они написаны
руками, а не сгенерированы.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

import httpx
from aiolimiter import AsyncLimiter
from loguru import logger
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from app.adapters.wb.models import (
    Card,
    CardsListResponse,
    Feedback,
    FeedbacksResponse,
    FunnelProduct,
    ItemRating,
    ItemRatingResponse,
    SalesFunnelResponse,
)

FEEDBACKS_HOST = "https://feedbacks-api.wildberries.ru"
FEEDBACKS_SANDBOX_HOST = "https://feedbacks-api-sandbox.wildberries.ru"
CONTENT_HOST = "https://content-api.wildberries.ru"
CONTENT_SANDBOX_HOST = "https://content-api-sandbox.wildberries.ru"
ANALYTICS_HOST = "https://seller-analytics-api.wildberries.ru"

CARDS_PAGE_SIZE = 100
"""Максимум карточек за запрос — жёсткий лимит метода."""

FEEDBACKS_PAGE_SIZE = 5000
"""Максимум отзывов за запрос. skip не может превышать 199 990."""

FEEDBACKS_MAX_SKIP = 199_990

ITEM_RATING_BATCH = 50
SALES_FUNNEL_BATCH = 1000


class WbError(Exception):
    """Wildberries ответил ошибкой в теле, а не кодом состояния."""


class WbAuthError(WbError):
    """401: токен отозван, истёк или не той категории.

    Отдельный тип, потому что реакция принципиально другая: повторять
    бессмысленно, прогон надо останавливать целиком и звать человека
    (design § D8).
    """


class WbReadOnlyError(WbError):
    """Запрос не входит в явный список методов получения данных."""


def _is_retryable(exc: BaseException) -> bool:
    """429 и 5xx повторяем, 401 — нет: токен отозван, повтор не поможет (design § D8)."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == httpx.codes.TOO_MANY_REQUESTS or code >= httpx.codes.INTERNAL_SERVER_ERROR
    return isinstance(exc, httpx.TransportError)


@dataclass(frozen=True, slots=True)
class RateLimits:
    """Лимиты запросов как (сколько, за сколько секунд).

    Значения по умолчанию — боевые, из официальных спецификаций.
    Вынесены в параметр по двум причинам: Wildberries их меняет, а тестам
    незачем честно ждать минуту между пачками.
    """

    feedbacks: tuple[int, float] = (3, 1)
    content: tuple[int, float] = (100, 60)
    analytics: tuple[int, float] = (3, 60)

    @classmethod
    def unthrottled(cls) -> RateLimits:
        """Для тестов: лимитеры на месте, но не тормозят."""
        return cls(feedbacks=(1000, 0.01), content=(1000, 0.01), analytics=(1000, 0.01))


class WbClient:
    """Один клиент на все категории. Лимитеры — по категориям, не по методам."""

    def __init__(
        self,
        token: str,
        *,
        content_token: str | None = None,
        analytics_token: str | None = None,
        sandbox: bool = False,
        timeout: float = 60.0,
        limits: RateLimits | None = None,
    ) -> None:
        self.feedbacks_host = FEEDBACKS_SANDBOX_HOST if sandbox else FEEDBACKS_HOST
        self.content_host = CONTENT_SANDBOX_HOST if sandbox else CONTENT_HOST
        self.analytics_host = ANALYTICS_HOST
        self._allowed_reads = frozenset(
            {
                ("GET", f"{self.feedbacks_host}/ping"),
                ("GET", f"{self.feedbacks_host}/api/v1/feedbacks"),
                ("GET", f"{self.feedbacks_host}/api/v1/feedbacks/archive"),
                ("POST", f"{self.content_host}/content/v2/get/cards/list"),
                ("POST", f"{self.content_host}/content/v2/get/cards/trash"),
                ("POST", f"{self.analytics_host}/api/analytics/v2/item-rating"),
                ("POST", f"{self.analytics_host}/api/analytics/v3/sales-funnel/products"),
            }
        )
        self._client = httpx.AsyncClient(timeout=timeout)
        self._tokens_by_host = {
            self.feedbacks_host: token,
            self.content_host: content_token or token,
            self.analytics_host: analytics_token or token,
        }
        limits = limits or RateLimits()
        self._feedbacks_limit = AsyncLimiter(*limits.feedbacks)
        self._content_limit = AsyncLimiter(*limits.content)
        self._analytics_limit = AsyncLimiter(*limits.analytics)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._client.aclose()

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential_jitter(initial=1, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    async def _request(
        self,
        method: str,
        url: str,
        limiter: AsyncLimiter,
        **kwargs: Any,
    ) -> Any:
        if (method.upper(), url) not in self._allowed_reads:
            raise WbReadOnlyError("Запрос WB не входит в список разрешённого чтения")
        origin = str(httpx.URL(url).copy_with(path="", query=None)).rstrip("/")
        auth_token = self._tokens_by_host[origin]
        async with limiter:
            response = await self._client.request(
                method, url, headers={"Authorization": auth_token}, **kwargs
            )
        if response.status_code == httpx.codes.UNAUTHORIZED:
            msg = (
                f"Wildberries вернул 401 на {url}. Токен отозван, истёк или выдан "
                f"без нужной категории — см. docs/03_КЛЮЧИ_И_ДОСТУПЫ.md § 1"
            )
            raise WbAuthError(msg)
        response.raise_for_status()
        return response.json()

    async def ping(self) -> Any:
        """Проверка живости токена — метод из спеки 01-general.yaml (TSK-002)."""
        return await self._request("GET", f"{self.feedbacks_host}/ping", self._feedbacks_limit)

    async def iter_cards(self) -> AsyncIterator[Card]:
        """Все карточки кабинета. Курсорная пагинация по паре (updatedAt, nmID).

        Признак последней страницы — карточек пришло меньше, чем просили.
        """
        cursor: dict[str, Any] = {"limit": CARDS_PAGE_SIZE}
        seen_versions: set[tuple[int, dt.datetime | None]] = set()
        seen_nm_ids: set[int] = set()
        while True:
            payload = {
                "settings": {
                    "cursor": cursor,
                    # -1 — любые карточки. Значение по умолчанию (0) вернёт
                    # только часть каталога, и половина товаров потеряется молча.
                    "filter": {"withPhoto": -1},
                    "sort": {"ascending": True},
                }
            }
            raw = await self._request(
                "POST",
                f"{self.content_host}/content/v2/get/cards/list",
                self._content_limit,
                json=payload,
            )
            page = CardsListResponse.model_validate(raw)
            for card in page.cards:
                version = (card.nm_id, card.updated_at)
                if version in seen_versions:
                    continue
                seen_versions.add(version)
                seen_nm_ids.add(card.nm_id)
                yield card
            if len(page.cards) < CARDS_PAGE_SIZE:
                logger.info("Карточки: выгружено {}", len(seen_nm_ids))
                return
            cursor = {
                "limit": CARDS_PAGE_SIZE,
                "updatedAt": page.cursor.updated_at,
                "nmID": page.cursor.nm_id,
            }

    async def iter_trash_cards(self) -> AsyncIterator[Card]:
        """Карточки в корзине, которых нет в обычном списке WB."""
        cursor: dict[str, Any] = {"limit": CARDS_PAGE_SIZE}
        while True:
            raw = await self._request(
                "POST",
                f"{self.content_host}/content/v2/get/cards/trash",
                self._content_limit,
                json={"settings": {"sort": {"ascending": True}, "cursor": cursor}},
            )
            page = CardsListResponse.model_validate(raw)
            for card in page.cards:
                yield card
            if len(page.cards) < CARDS_PAGE_SIZE:
                return
            if page.cursor.trashed_at is None or page.cursor.nm_id is None:
                raise WbError("WB не вернул курсор для следующей страницы корзины")
            cursor = {
                "limit": CARDS_PAGE_SIZE,
                "trashedAt": page.cursor.trashed_at,
                "nmID": page.cursor.nm_id,
            }

    async def iter_feedbacks(
        self,
        *,
        date_from: dt.datetime,
        date_to: dt.datetime | None = None,
    ) -> AsyncIterator[Feedback]:
        """Отзывы за период — оба состояния обработки.

        `isAnswered` в этом методе обязателен, и «оба сразу» он не умеет:
        нужны два прохода, иначе половина отзывов не придёт.
        """
        for is_answered in (False, True):
            async for feedback in self._iter_feedbacks_page(
                is_answered=is_answered,
                date_from=date_from,
                date_to=date_to,
            ):
                yield feedback

    async def _iter_feedbacks_page(
        self,
        *,
        is_answered: bool,
        date_from: dt.datetime,
        date_to: dt.datetime | None,
    ) -> AsyncIterator[Feedback]:
        skip = 0
        while True:
            params: dict[str, Any] = {
                "isAnswered": str(is_answered).lower(),
                "take": FEEDBACKS_PAGE_SIZE,
                "skip": skip,
                "order": "dateDesc",
                "dateFrom": int(date_from.timestamp()),
            }
            if date_to is not None:
                params["dateTo"] = int(date_to.timestamp())
            raw = await self._request(
                "GET",
                f"{self.feedbacks_host}/api/v1/feedbacks",
                self._feedbacks_limit,
                params=params,
            )
            page = FeedbacksResponse.model_validate(raw)
            if page.error:
                raise WbError(page.error_text or "Wildberries вернул error=true")
            for feedback in page.data.feedbacks:
                yield feedback
            if len(page.data.feedbacks) < FEEDBACKS_PAGE_SIZE:
                return
            skip += FEEDBACKS_PAGE_SIZE
            if skip > FEEDBACKS_MAX_SKIP:
                logger.warning(
                    "Достигнут предел пагинации отзывов (skip={}), сузьте период",
                    FEEDBACKS_MAX_SKIP,
                )
                return

    async def iter_archive_feedbacks(self, *, since: dt.datetime) -> AsyncIterator[Feedback]:
        """Архивные отзывы. В обычном списке они не видны — нужен отдельный проход.

        Фильтра по дате у метода нет, поэтому идём от свежих к старым
        (`order=dateDesc`) и останавливаемся, как только вышли за период.
        """
        skip = 0
        while True:
            raw = await self._request(
                "GET",
                f"{self.feedbacks_host}/api/v1/feedbacks/archive",
                self._feedbacks_limit,
                params={"take": FEEDBACKS_PAGE_SIZE, "skip": skip, "order": "dateDesc"},
            )
            page = FeedbacksResponse.model_validate(raw)
            if page.error:
                raise WbError(page.error_text or "Wildberries вернул error=true")
            for feedback in page.data.feedbacks:
                if feedback.created_date < since:
                    return
                yield feedback
            if len(page.data.feedbacks) < FEEDBACKS_PAGE_SIZE:
                return
            skip += FEEDBACKS_PAGE_SIZE
            if skip > FEEDBACKS_MAX_SKIP:
                return

    async def fetch_item_rating(
        self,
        nm_ids: Sequence[int],
        *,
        period_start: dt.date,
        period_end: dt.date,
    ) -> list[ItemRating]:
        """Рейтинг и прирост оценок по товарам. TSK-105.

        Пачками по 50 — больше метод не принимает. При 3 запросах в минуту
        5000 артикулов собираются около 34 минут, в ночное окно укладываемся.
        Наивный запрос «по одному артикулу» превратит это в сутки.

        Период не глубже 364 суток от вчерашнего дня.
        """
        items: list[ItemRating] = []
        for start in range(0, len(nm_ids), ITEM_RATING_BATCH):
            batch = list(nm_ids[start : start + ITEM_RATING_BATCH])
            raw = await self._request(
                "POST",
                f"{self.analytics_host}/api/analytics/v2/item-rating",
                self._analytics_limit,
                json={
                    "currentPeriod": {
                        "start": period_start.isoformat(),
                        "end": period_end.isoformat(),
                    },
                    "nmIds": batch,
                    # orderBy и offset обязательны, без них метод отвечает 400.
                    "orderBy": {"field": "feedbackCount", "mode": "desc"},
                    "offset": 0,
                    "limit": ITEM_RATING_BATCH,
                },
            )
            items.extend(ItemRatingResponse.model_validate(raw).data.items)
        logger.info("Оценка товара: получено строк {}", len(items))
        return items

    async def fetch_sales_funnel(
        self,
        nm_ids: Sequence[int],
        *,
        period_start: dt.date,
        period_end: dt.date,
    ) -> list[FunnelProduct]:
        """Воронка продаж: заказы и выкупы за период. TSK-106.

        До 1000 артикулов за запрос, 3 запроса в минуту — на 5000 артикулов
        это пять запросов.
        """
        products: list[FunnelProduct] = []
        for start in range(0, len(nm_ids), SALES_FUNNEL_BATCH):
            batch = list(nm_ids[start : start + SALES_FUNNEL_BATCH])
            raw = await self._request(
                "POST",
                f"{self.analytics_host}/api/analytics/v3/sales-funnel/products",
                self._analytics_limit,
                json={
                    "selectedPeriod": {
                        "start": period_start.isoformat(),
                        "end": period_end.isoformat(),
                    },
                    "nmIds": batch,
                    "limit": len(batch),
                    "offset": 0,
                },
            )
            products.extend(SalesFunnelResponse.model_validate(raw).data.products)
        logger.info("Воронка продаж: получено строк {}", len(products))
        return products
