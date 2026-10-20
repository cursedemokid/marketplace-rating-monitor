"""Модели ответов Wildberries — только те поля, которые мы действительно читаем.

Почему не генерация из OpenAPI, как планировалось в docs/05 § 4.1:
`dev.wildberries.ru` отвечает HTTP 498 на любой запрос не из браузера
(проверено 24.09.2026, в том числе с браузерным User-Agent). Скачать спеку
командой или в CI нельзя — только руками через браузер. Держать 600 КБ
сгенерированного кода ради полутора десятков полей смысла нет, поэтому
описываем узкий контракт руками, а сверяем его тестами на фикстурах.

Рядом с каждым полем — путь в спецификации, чтобы сверка не превращалась
в археологию:
  02-items.yaml         → POST /content/v2/get/cards/list
  09-communications.yaml → GET /api/v1/feedbacks, GET /api/v1/feedbacks/archive
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    # extra="ignore": Wildberries добавляет поля молча, и падать из-за
    # нового поля в ответе мы не хотим.
    model_config = ConfigDict(extra="ignore", frozen=True)


class CardSize(_Model):
    tech_size: str | None = Field(default=None, alias="techSize")


class Card(_Model):
    """Карточка товара. cards[] в ответе /content/v2/get/cards/list."""

    nm_id: int = Field(alias="nmID")
    vendor_code: str = Field(alias="vendorCode")
    brand: str | None = None
    title: str | None = None
    subject_name: str | None = Field(default=None, alias="subjectName")
    updated_at: dt.datetime | None = Field(default=None, alias="updatedAt")
    sizes: list[CardSize] = Field(default_factory=list)

    @property
    def size(self) -> str | None:
        """Первый техразмер. У товаров без размеров — None."""
        for item in self.sizes:
            if item.tech_size:
                return item.tech_size
        return None


class CardsCursor(_Model):
    """Пагинатор. Курсорная пагинация: следующий запрос идёт от этой пары."""

    updated_at: str | None = Field(default=None, alias="updatedAt")
    trashed_at: str | None = Field(default=None, alias="trashedAt")
    nm_id: int | None = Field(default=None, alias="nmID")
    total: int = 0


class CardsListResponse(_Model):
    cards: list[Card] = Field(default_factory=list)
    cursor: CardsCursor = Field(default_factory=CardsCursor)


class ProductDetails(_Model):
    nm_id: int = Field(alias="nmId")


class Feedback(_Model):
    """Отзыв. Одинаков для /feedbacks и /feedbacks/archive."""

    id: str
    text: str = ""
    pros: str = ""
    cons: str = ""
    valuation: int = Field(alias="productValuation")
    created_date: dt.datetime = Field(alias="createdDate")
    product_details: ProductDetails = Field(alias="productDetails")

    @property
    def nm_id(self) -> int:
        return self.product_details.nm_id


class FeedbacksData(_Model):
    feedbacks: list[Feedback] = Field(default_factory=list)


class FeedbacksResponse(_Model):
    data: FeedbacksData = Field(default_factory=FeedbacksData)
    error: bool = False
    error_text: str = Field(default="", alias="errorText")


class RatingValue(_Model):
    """Значение с динамикой. `current` у звёзд — ПРИРОСТ за период, не итог."""

    current: float = 0
    dynamics: float = 0


class RatingCount(_Model):
    current: int = 0
    dynamics: float = 0


class ItemRating(_Model):
    """Строка ответа /api/analytics/v2/item-rating (DistributionTableItem).

    ⚠️ `five_star.current` и соседние — прирост оценок ЗА ПЕРИОД запроса,
    а не «всего пятёрок у товара». Накопленного итога по товару этот метод
    не отдаёт вовсе: поле `total` есть только в агрегате по продавцу
    (`data.feedbackIncrease`). Перепутать — значит получить неверную
    структуру оценок во всей системе.
    """

    nm_id: int = Field(alias="nmId")
    vendor_code: str = Field(default="", alias="vendorCode")
    feedback_rating: RatingValue = Field(default_factory=RatingValue, alias="feedbackRating")
    feedback_count: RatingCount = Field(default_factory=RatingCount, alias="feedbackCount")
    five_star: RatingCount = Field(default_factory=RatingCount, alias="fiveStar")
    four_star: RatingCount = Field(default_factory=RatingCount, alias="fourStar")
    three_star: RatingCount = Field(default_factory=RatingCount, alias="threeStar")
    two_star: RatingCount = Field(default_factory=RatingCount, alias="twoStar")
    one_star: RatingCount = Field(default_factory=RatingCount, alias="oneStar")


class ItemRatingData(_Model):
    items: list[ItemRating] = Field(default_factory=list)


class ItemRatingResponse(_Model):
    data: ItemRatingData = Field(default_factory=ItemRatingData)


class Conversions(_Model):
    buyout_percent: int = Field(default=0, alias="buyoutPercent")
    """ЦЕЛЫЙ процент. Порог ТЗ в 0,2 п.п. по нему не различить — считаем сами."""


class FunnelStatistic(_Model):
    order_count: int = Field(default=0, alias="orderCount")
    buyout_count: int = Field(default=0, alias="buyoutCount")
    conversions: Conversions = Field(default_factory=Conversions)


class FunnelStatistics(_Model):
    selected: FunnelStatistic = Field(default_factory=FunnelStatistic)


class FunnelItem(_Model):
    nm_id: int = Field(alias="nmId")


class FunnelProduct(_Model):
    product: FunnelItem
    statistic: FunnelStatistics = Field(default_factory=FunnelStatistics)


class FunnelData(_Model):
    products: list[FunnelProduct] = Field(default_factory=list)


class SalesFunnelResponse(_Model):
    data: FunnelData = Field(default_factory=FunnelData)
