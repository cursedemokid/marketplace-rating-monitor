"""Отзывы. TSK-104.

Первичный ключ — идентификатор от Wildberries, поэтому повторный проход
по тому же периоду не создаёт дублей, а обновляет текст: отзыв можно
отредактировать, и мы хотим видеть актуальную версию.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class FeedbackRow:
    id: str
    nm_id: int
    valuation: int
    text: str
    pros: str
    cons: str
    created_at: dt.datetime
    created_date: dt.date


_UPSERT = text("""
    insert into wb.feedbacks
        (id, nm_id, valuation, text, pros, cons, created_at, created_date, imported_at)
    values
        (:id, :nm_id, :valuation, :text, :pros, :cons, :created_at, :created_date, now())
    on conflict (id) do update set
        valuation = excluded.valuation,
        text      = excluded.text,
        pros      = excluded.pros,
        cons      = excluded.cons
""")


class FeedbackRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_many(self, rows: Sequence[FeedbackRow]) -> int:
        if not rows:
            return 0
        await self._session.execute(
            _UPSERT,
            [
                {
                    "id": row.id,
                    "nm_id": row.nm_id,
                    "valuation": row.valuation,
                    "text": row.text,
                    "pros": row.pros,
                    "cons": row.cons,
                    "created_at": row.created_at,
                    "created_date": row.created_date,
                }
                for row in rows
            ],
        )
        return len(rows)

    async def count(self) -> int:
        result = await self._session.execute(text("select count(*) from wb.feedbacks"))
        return int(result.scalar_one())

    async def known_nm_ids(self, account_key: str | None = None) -> set[int]:
        """Товары, которые уже есть в справочнике: отзыв на чужой nm_id класть некуда."""
        query = "select nm_id from wb.products"
        params: dict[str, str] = {}
        if account_key is not None:
            query += " where account_key = :account_key"
            params["account_key"] = account_key
        result = await self._session.execute(text(query), params)
        return {int(row[0]) for row in result.all()}
