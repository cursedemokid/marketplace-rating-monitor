"""Утверждённые причины и ещё не классифицированные отзывы."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class PendingReview:
    feedback_id: str
    body: str
    stars: int
    product_title: str = ""
    product_size: str = ""


class ReasonRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def approved(self) -> dict[int, str]:
        result = await self._session.execute(
            text("""
                select id, name from wb.negative_reasons
                 where is_active and is_approved order by id
            """)
        )
        return {int(row.id): str(row.name) for row in result.all()}

    async def top_reason(
        self, *, nm_id: int, period_start: dt.date, period_end: dt.date
    ) -> str | None:
        """Наиболее частая подтверждённая причина в негативных отзывах периода."""
        result = await self._session.execute(
            text("""
                select r.name
                  from wb.feedbacks f
                  join wb.feedback_reasons fr on fr.feedback_id = f.id
                  join wb.negative_reasons r on r.id = fr.reason_id
                 where f.nm_id = :nm_id
                   and f.valuation <= 3
                   and f.created_date between :period_start and :period_end
                   and r.is_active and r.is_approved
                   and not r.is_external
                   and r.name <> 'причина не указана'
                 group by r.id, r.name
                 order by count(*) desc, r.name
                 limit 1
            """),
            {"nm_id": nm_id, "period_start": period_start, "period_end": period_end},
        )
        value = result.scalar_one_or_none()
        return str(value) if value is not None else None

    async def pending(
        self,
        *,
        until: dt.date,
        max_stars: int,
        limit: int,
        feedback_ids: list[str] | None = None,
    ) -> list[PendingReview]:
        result = await self._session.execute(
            text("""
                select f.id, f.valuation, coalesce(p.title, '') as product_title,
                       coalesce(p.size, '') as product_size,
                       concat_ws(E'\n', nullif(f.text, ''), nullif(f.pros, ''),
                                 nullif(f.cons, '')) as body
                  from wb.feedbacks f
                  join wb.products p on p.nm_id = f.nm_id
                 where f.created_date <= :until
                   and f.valuation <= :max_stars
                   and (cast(:feedback_ids as text[]) is null
                        or f.id = any(cast(:feedback_ids as text[])))
                   and not exists (
                       select 1 from wb.feedback_reasons fr where fr.feedback_id = f.id
                   )
                   and not exists (
                       select 1 from wb.ai_review_claims c where c.feedback_id = f.id
                   )
                 order by f.created_at, f.id
                 limit :limit
                 for update of f skip locked
            """),
            {
                "until": until,
                "max_stars": max_stars,
                "limit": limit,
                "feedback_ids": feedback_ids,
            },
        )
        return [
            PendingReview(
                str(row.id),
                str(row.body or ""),
                int(row.valuation),
                str(row.product_title or ""),
                str(row.product_size or ""),
            )
            for row in result.all()
        ]

    async def pending_retry(
        self,
        *,
        until: dt.date,
        max_stars: int,
        limit: int,
        feedback_ids: list[str],
    ) -> list[PendingReview]:
        """Select explicitly requested claimed reviews eligible for their one paid retry."""
        if not feedback_ids:
            return []
        result = await self._session.execute(
            text("""
                select f.id, f.valuation, coalesce(p.title, '') as product_title,
                       coalesce(p.size, '') as product_size,
                       concat_ws(E'\n', nullif(f.text, ''), nullif(f.pros, ''),
                                 nullif(f.cons, '')) as body
                  from wb.feedbacks f
                  join wb.products p on p.nm_id = f.nm_id
                 where f.created_date <= :until
                   and f.valuation <= :max_stars
                   and f.id = any(cast(:feedback_ids as text[]))
                   and not exists (
                       select 1 from wb.feedback_reasons fr where fr.feedback_id = f.id
                   )
                   and exists (
                       select 1 from wb.ai_review_claims c where c.feedback_id = f.id
                   )
                   and not exists (
                       select 1 from wb.ai_review_retries r where r.feedback_id = f.id
                   )
                 order by f.created_at, f.id
                 limit :limit
                 for update of f skip locked
            """),
            {
                "until": until,
                "max_stars": max_stars,
                "limit": limit,
                "feedback_ids": feedback_ids,
            },
        )
        return [
            PendingReview(
                str(row.id),
                str(row.body or ""),
                int(row.valuation),
                str(row.product_title or ""),
                str(row.product_size or ""),
            )
            for row in result.all()
        ]

    async def count_pending(
        self,
        *,
        until: dt.date,
        max_stars: int,
        feedback_ids: list[str] | None,
    ) -> int:
        result = await self._session.execute(
            text("""
                select count(*)
                  from wb.feedbacks f
                 where f.created_date <= :until and f.valuation <= :max_stars
                   and (cast(:feedback_ids as text[]) is null
                        or f.id = any(cast(:feedback_ids as text[])))
                   and not exists (
                       select 1 from wb.feedback_reasons fr where fr.feedback_id = f.id
                   )
                   and not exists (
                       select 1 from wb.ai_review_claims c where c.feedback_id = f.id
                   )
            """),
            {"until": until, "max_stars": max_stars, "feedback_ids": feedback_ids},
        )
        return int(result.scalar_one())

    async def count_pending_retry(
        self, *, until: dt.date, max_stars: int, feedback_ids: list[str]
    ) -> int:
        if not feedback_ids:
            return 0
        result = await self._session.execute(
            text("""
                select count(*)
                  from wb.feedbacks f
                 where f.created_date <= :until and f.valuation <= :max_stars
                   and f.id = any(cast(:feedback_ids as text[]))
                   and not exists (
                       select 1 from wb.feedback_reasons fr where fr.feedback_id = f.id
                   )
                   and exists (
                       select 1 from wb.ai_review_claims c where c.feedback_id = f.id
                   )
                   and not exists (
                       select 1 from wb.ai_review_retries r where r.feedback_id = f.id
                   )
            """),
            {"until": until, "max_stars": max_stars, "feedback_ids": feedback_ids},
        )
        return int(result.scalar_one())

    async def save(
        self,
        assignments: list[tuple[str, int]],
        *,
        model: str,
        classified_at: dt.datetime,
    ) -> None:
        await self._session.execute(
            text("""
                insert into wb.feedback_reasons
                    (feedback_id, reason_id, confidence, model, classified_at)
                values (:feedback_id, :reason_id, null, :model, :classified_at)
                on conflict (feedback_id, reason_id) do nothing
            """),
            [
                {
                    "feedback_id": feedback_id,
                    "reason_id": reason_id,
                    "model": model,
                    "classified_at": classified_at,
                }
                for feedback_id, reason_id in assignments
            ],
        )
