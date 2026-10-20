"""Снимки показателей на дату — ядро системы. TSK-107.

Первичный ключ `(nm_id, snapshot_date)` делает повторный прогон
безопасным: снимок перезаписывается, а не дублируется (FR-1.3). Это же
нужно и по существу — аналитика Wildberries досчитывается задним числом,
поэтому вчерашняя цифра выкупа завтра может измениться (FR-1.5).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class SnapshotRow:
    nm_id: int
    snapshot_date: dt.date
    rating: float | None
    ratings_total: int
    ratings_5: int
    ratings_4: int
    ratings_3: int
    ratings_2: int
    ratings_1: int
    buyout_percent: float | None
    orders_count: int | None
    buyouts_count: int | None
    rating_complete: bool = True
    funnel_complete: bool = True


@dataclass(frozen=True, slots=True)
class RatingCounts:
    """Накопленные счётчики оценок по данным нашей базы отзывов."""

    total: int
    five: int
    four: int
    three: int
    two: int
    one: int


_UPSERT = text("""
    insert into wb.daily_snapshots (
        nm_id, snapshot_date, rating, ratings_total,
        ratings_5, ratings_4, ratings_3, ratings_2, ratings_1,
        buyout_percent, orders_count, buyouts_count, collected_at
    ) values (
        :nm_id, :snapshot_date, :rating, :ratings_total,
        :ratings_5, :ratings_4, :ratings_3, :ratings_2, :ratings_1,
        :buyout_percent, :orders_count, :buyouts_count, now()
    )
    on conflict (nm_id, snapshot_date) do update set
        rating         = case when :rating_complete then excluded.rating
                              else wb.daily_snapshots.rating end,
        ratings_total  = excluded.ratings_total,
        ratings_5      = excluded.ratings_5,
        ratings_4      = excluded.ratings_4,
        ratings_3      = excluded.ratings_3,
        ratings_2      = excluded.ratings_2,
        ratings_1      = excluded.ratings_1,
        buyout_percent = case when :funnel_complete then excluded.buyout_percent
                              else wb.daily_snapshots.buyout_percent end,
        orders_count   = case when :funnel_complete then excluded.orders_count
                              else wb.daily_snapshots.orders_count end,
        buyouts_count  = case when :funnel_complete then excluded.buyouts_count
                              else wb.daily_snapshots.buyouts_count end,
        collected_at   = now()
""")

_COUNTS = text("""
    select nm_id,
           count(*)                                as total,
           count(*) filter (where valuation = 5)   as five,
           count(*) filter (where valuation = 4)   as four,
           count(*) filter (where valuation = 3)   as three,
           count(*) filter (where valuation = 2)   as two,
           count(*) filter (where valuation = 1)   as one
      from wb.feedbacks
     where created_date <= :on_date
     group by nm_id
""")


class SnapshotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def counts_as_of(self, on_date: dt.date) -> dict[int, RatingCounts]:
        """Накопленные счётчики оценок на дату — из своей базы отзывов.

        Почему не из `item-rating`: там звёзды приходят приростом за период,
        накопленного итога по товару метод не отдаёт. Свои отзывы дают точный
        разрез по дням и не зависят от того, какой период мы запросили.
        """
        result = await self._session.execute(_COUNTS, {"on_date": on_date})
        return {
            int(row.nm_id): RatingCounts(
                total=int(row.total),
                five=int(row.five),
                four=int(row.four),
                three=int(row.three),
                two=int(row.two),
                one=int(row.one),
            )
            for row in result.all()
        }

    async def upsert_many(self, rows: Sequence[SnapshotRow]) -> int:
        if not rows:
            return 0
        await self._session.execute(
            _UPSERT,
            [
                {
                    "nm_id": row.nm_id,
                    "snapshot_date": row.snapshot_date,
                    "rating": row.rating,
                    "ratings_total": row.ratings_total,
                    "ratings_5": row.ratings_5,
                    "ratings_4": row.ratings_4,
                    "ratings_3": row.ratings_3,
                    "ratings_2": row.ratings_2,
                    "ratings_1": row.ratings_1,
                    "buyout_percent": row.buyout_percent,
                    "orders_count": row.orders_count,
                    "buyouts_count": row.buyouts_count,
                    "rating_complete": row.rating_complete,
                    "funnel_complete": row.funnel_complete,
                }
                for row in rows
            ],
        )
        return len(rows)

    async def count_for_date(self, on_date: dt.date) -> int:
        result = await self._session.execute(
            text("select count(*) from wb.daily_snapshots where snapshot_date = :d"),
            {"d": on_date},
        )
        return int(result.scalar_one())
