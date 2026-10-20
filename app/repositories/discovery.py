"""Хранение кластеров новых причин до решения администратора."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class DiscoveryReview:
    feedback_id: str
    body: str


@dataclass(frozen=True, slots=True)
class DiscoveryCandidate:
    id: int
    name: str | None
    status: str
    feedback_ids: tuple[str, ...]
    merged_into: int | None = None


class DiscoveryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def repeated_unassigned(self, *, until: dt.date, limit: int) -> list[DiscoveryReview]:
        result = await self._session.execute(
            text("""
                select f.id,
                       concat_ws(E'\\n', nullif(f.text, ''), nullif(f.pros, ''),
                                 nullif(f.cons, '')) as body
                  from wb.feedbacks f
                  join wb.feedback_reasons fr on fr.feedback_id = f.id
                  join wb.negative_reasons r on r.id = fr.reason_id
                 where f.created_date <= :until
                   and f.valuation <= 3
                   and r.name = 'причина не указана'
                   and (nullif(f.text, '') is not null
                        or nullif(f.pros, '') is not null
                        or nullif(f.cons, '') is not null)
                   and not exists (
                       select 1 from wb.discovery_candidate_reviews dcr
                        where dcr.feedback_id = f.id
                   )
                   and not exists (
                       select 1 from wb.discovery_examined_reviews der
                        where der.feedback_id = f.id
                   )
                 order by f.created_date, f.id
                 limit :limit
                 for update of f skip locked
            """),
            {"until": until, "limit": limit},
        )
        return [DiscoveryReview(str(row.id), str(row.body or "")) for row in result.all()]

    async def mark_examined(self, feedback_ids: list[str]) -> None:
        if not feedback_ids:
            return
        await self._session.execute(
            text("""
                insert into wb.discovery_examined_reviews (feedback_id)
                select feedback_id from unnest(cast(:feedback_ids as text[])) as ids(feedback_id)
                on conflict (feedback_id) do nothing
            """),
            {"feedback_ids": list(dict.fromkeys(feedback_ids))},
        )

    async def create_candidates(
        self,
        groups: list[list[str]],
        *,
        names: list[str | None] | None = None,
    ) -> list[int]:
        candidate_ids: list[int] = []
        names = names or [None] * len(groups)
        for group, proposed_name in zip(groups, names, strict=True):
            feedback_ids = list(dict.fromkeys(group))
            if not feedback_ids:
                continue
            overlap = await self._session.execute(
                text("""
                    select exists(select 1 from wb.discovery_candidate_reviews
                                  where feedback_id = any(:feedback_ids))
                """),
                {"feedback_ids": feedback_ids},
            )
            if overlap.scalar_one():
                continue
            candidate_id = int(
                (
                    await self._session.execute(
                        text("""
                            insert into wb.discovery_candidates (proposed_name)
                            values (:name) returning id
                        """),
                        {"name": proposed_name or None},
                    )
                ).scalar_one()
            )
            await self._session.execute(
                text("""
                    insert into wb.discovery_candidate_reviews (candidate_id, feedback_id)
                    select :candidate_id, feedback_id
                      from unnest(cast(:feedback_ids as text[])) as ids(feedback_id)
                """),
                {"candidate_id": candidate_id, "feedback_ids": feedback_ids},
            )
            candidate_ids.append(candidate_id)
        return candidate_ids

    async def candidates(self, *, status: str | None = None) -> list[DiscoveryCandidate]:
        result = await self._session.execute(
            text("""
                select c.id, c.proposed_name, c.status, c.merged_into,
                       coalesce(array_agg(dcr.feedback_id order by dcr.feedback_id)
                                filter (where dcr.feedback_id is not null), '{}') as feedback_ids
                  from wb.discovery_candidates c
                  left join wb.discovery_candidate_reviews dcr on dcr.candidate_id = c.id
                 where cast(:status as text) is null or c.status = :status
                 group by c.id
                 order by c.id
            """),
            {"status": status},
        )
        return [
            DiscoveryCandidate(
                int(row.id),
                str(row.proposed_name) if row.proposed_name is not None else None,
                str(row.status),
                tuple(str(value) for value in row.feedback_ids),
                int(row.merged_into) if row.merged_into is not None else None,
            )
            for row in result.all()
        ]

    async def rename(self, candidate_id: int, name: str) -> None:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Название причины не может быть пустым")
        result = await self._session.execute(
            text("""
                update wb.discovery_candidates
                   set proposed_name = :name, updated_at = now()
                 where id = :id and status = 'pending'
                returning id
            """),
            {"id": candidate_id, "name": clean_name},
        )
        if result.scalar_one_or_none() is None:
            raise ValueError(f"Кандидат {candidate_id} не найден или уже обработан")

    async def approve(self, candidate_id: int, *, approved_by: str) -> int:
        row = (
            await self._session.execute(
                text("""
                    select proposed_name from wb.discovery_candidates
                     where id = :id and status = 'pending'
                     for update
                """),
                {"id": candidate_id},
            )
        ).first()
        if row is None or not str(row.proposed_name or "").strip():
            raise ValueError("Для одобрения задайте название ожидающему кандидату")
        reason_id = int(
            (
                await self._session.execute(
                    text("""
                        insert into wb.negative_reasons
                            (name, is_active, is_approved, created_by)
                        values (:name, true, true, :created_by)
                        returning id
                    """),
                    {"name": str(row.proposed_name).strip(), "created_by": f"user:{approved_by}"},
                )
            ).scalar_one()
        )
        fallback_id = (
            await self._session.execute(
                text("select id from wb.negative_reasons where name = 'причина не указана'")
            )
        ).scalar_one()
        await self._session.execute(
            text("""
                delete from wb.feedback_reasons fr
                 using wb.discovery_candidate_reviews dcr
                 where dcr.candidate_id = :id
                   and fr.feedback_id = dcr.feedback_id
                   and fr.reason_id = :fallback_id
            """),
            {"id": candidate_id, "fallback_id": fallback_id},
        )
        await self._session.execute(
            text("""
                insert into wb.feedback_reasons
                    (feedback_id, reason_id, model, classified_at)
                select feedback_id, :reason_id, 'discovery:admin', now()
                  from wb.discovery_candidate_reviews
                 where candidate_id = :id
                on conflict (feedback_id, reason_id) do nothing
            """),
            {"id": candidate_id, "reason_id": reason_id},
        )
        await self._session.execute(
            text("""
                update wb.discovery_candidates
                   set status = 'approved', resolved_at = now(), updated_at = now()
                 where id = :id
            """),
            {"id": candidate_id},
        )
        return reason_id

    async def reject(self, candidate_id: int) -> None:
        result = await self._session.execute(
            text("""
                update wb.discovery_candidates
                   set status = 'rejected', resolved_at = now(), updated_at = now()
                 where id = :id and status = 'pending'
                returning id
            """),
            {"id": candidate_id},
        )
        if result.scalar_one_or_none() is None:
            raise ValueError(f"Кандидат {candidate_id} не найден или уже обработан")

    async def merge(self, source_id: int, target_id: int) -> None:
        if source_id == target_id:
            raise ValueError("Нельзя объединить кандидата с самим собой")
        target = (
            await self._session.execute(
                text("""
                    select id from wb.discovery_candidates
                     where id = :id and status = 'pending' for update
                """),
                {"id": target_id},
            )
        ).scalar_one_or_none()
        source = (
            await self._session.execute(
                text("""
                    select id from wb.discovery_candidates
                     where id = :id and status = 'pending' for update
                """),
                {"id": source_id},
            )
        ).scalar_one_or_none()
        if target is None or source is None:
            raise ValueError("Оба кандидата должны ожидать модерации")
        await self._session.execute(
            text("""
                update wb.discovery_candidate_reviews
                   set candidate_id = :target
                 where candidate_id = :source
            """),
            {"target": target_id, "source": source_id},
        )
        await self._session.execute(
            text("""
                update wb.discovery_candidates
                   set status = 'merged', merged_into = :target,
                       resolved_at = now(), updated_at = now()
                 where id = :source
            """),
            {"source": source_id, "target": target_id},
        )
