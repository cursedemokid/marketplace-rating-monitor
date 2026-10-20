"""Read candidate examples and save suggestions without approving them."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class AiNamingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def pending_candidate_ids(self, *, limit: int) -> list[int]:
        result = await self._session.execute(
            text("""
                select c.id
                  from wb.discovery_candidates c
                 where c.status = 'pending' and c.proposed_name is null
                   and not exists (
                       select 1 from wb.ai_naming_requests n
                        where n.candidate_id = c.id
                   )
                 order by c.id
                 limit :limit
            """),
            {"limit": limit},
        )
        return [int(value) for value in result.scalars().all()]

    async def sample_texts(self, candidate_id: int, *, limit: int) -> list[str]:
        result = await self._session.execute(
            text("""
                select concat_ws(E'\\n', nullif(f.text, ''), nullif(f.pros, ''),
                                 nullif(f.cons, '')) as body
                  from wb.discovery_candidates c
                  join wb.discovery_candidate_reviews dcr on dcr.candidate_id = c.id
                  join wb.feedbacks f on f.id = dcr.feedback_id
                 where c.id = :candidate_id and c.status = 'pending'
                   and c.proposed_name is null
                 order by dcr.feedback_id
                 limit :limit
            """),
            {"candidate_id": candidate_id, "limit": limit},
        )
        return [str(row.body or "") for row in result.all()]

    async def save_proposed_name(self, candidate_id: int, name: str) -> bool:
        result = await self._session.execute(
            text("""
                update wb.discovery_candidates
                   set proposed_name = :name, updated_at = now()
                 where id = :candidate_id and status = 'pending'
                   and proposed_name is null
                returning id
            """),
            {"candidate_id": candidate_id, "name": name},
        )
        return result.scalar_one_or_none() is not None
