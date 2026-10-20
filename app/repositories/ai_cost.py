"""Atomic project-wide AI budget reservations and per-call usage ledger."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MONTHLY_PROJECT_CAP_USD = Decimal("100.00")
_BUDGET_LOCK_KEY = 8_314_002_271_553_002
_PROJECT_TIMEZONE = ZoneInfo("Europe/Moscow")


def calendar_month_start(value: dt.datetime) -> dt.date:
    """Return the first day of the timestamp's Moscow calendar month."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("reservation timestamp must be timezone-aware")
    return value.astimezone(_PROJECT_TIMEZONE).date().replace(day=1)


class CostLimitExceededError(RuntimeError):
    """The next worst-case call would exceed this month's project cap."""


class ReviewAlreadyClaimedError(RuntimeError):
    """At least one review is already being or has been sent for classification."""


class RetryNotAllowedError(RuntimeError):
    """A paid retry was requested for an ineligible or already retried review."""


class AiCostRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def reserve(
        self,
        *,
        model: str,
        feedback_ids: list[str],
        estimated_cost_usd: Decimal,
        reserved_at: dt.datetime,
        allow_uncertain_manual: bool = False,
        classifier_prompt_sha256: str | None = None,
        classifier_batch_size: int | None = None,
    ) -> int:
        if estimated_cost_usd < 0:
            raise ValueError("estimated cost cannot be negative")
        if not feedback_ids:
            raise ValueError("cannot reserve an empty review batch")
        if len(set(feedback_ids)) != len(feedback_ids):
            raise ValueError("review batch contains duplicate feedback ids")

        # Serialize admission decisions across processes and sessions. Reservation
        # is committed before the HTTP call, so a concurrent worker sees it.
        await self._session.execute(
            text("select pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _BUDGET_LOCK_KEY},
        )
        month_start = calendar_month_start(reserved_at)
        await self._ensure_budget_available(month_start, estimated_cost_usd)

        claimed = await self._session.execute(
            text(
                "select feedback_id from wb.ai_review_claims "
                "where feedback_id = any(cast(:ids as text[]))"
            ),
            {"ids": feedback_ids},
        )
        if claimed.first() is not None:
            await self._session.rollback()
            raise ReviewAlreadyClaimedError("one or more reviews have already been claimed")

        result = await self._session.execute(
            text("""
                insert into wb.ai_cost_requests
                    (model, month_start, status, estimated_cost_usd, reserved_at,
                     classifier_prompt_sha256, classifier_batch_size)
                values (:model, :month_start, 'reserved', :estimated_cost_usd, :reserved_at,
                        :classifier_prompt_sha256, :classifier_batch_size)
                returning id
            """),
            {
                "model": model,
                "month_start": month_start,
                "estimated_cost_usd": estimated_cost_usd,
                "reserved_at": reserved_at,
                "classifier_prompt_sha256": classifier_prompt_sha256,
                "classifier_batch_size": classifier_batch_size,
            },
        )
        request_id = int(result.scalar_one())
        await self._session.execute(
            text("""
                insert into wb.ai_review_claims (feedback_id, request_id, claimed_at)
                select unnest(cast(:ids as text[])), :request_id, :claimed_at
            """),
            {"ids": feedback_ids, "request_id": request_id, "claimed_at": reserved_at},
        )
        if allow_uncertain_manual:
            await self._session.execute(
                text("""
                    insert into wb.ai_uncertain_admissions
                        (request_id, uncertain_request_id, admitted_at)
                    select :request_id, id, :admitted_at
                      from wb.ai_cost_requests
                     where month_start = :month_start and status = 'uncertain'
                """),
                {
                    "request_id": request_id,
                    "admitted_at": reserved_at,
                    "month_start": month_start,
                },
            )
        await self._session.commit()
        return request_id

    async def reserve_candidate_name(
        self,
        *,
        candidate_id: int,
        model: str,
        estimated_cost_usd: Decimal,
        reserved_at: dt.datetime,
    ) -> int | None:
        """Reserve one paid name proposal; a candidate can only be claimed once."""
        if estimated_cost_usd < 0:
            raise ValueError("estimated cost cannot be negative")
        await self._session.execute(
            text("select pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _BUDGET_LOCK_KEY},
        )
        month_start = calendar_month_start(reserved_at)
        await self._ensure_budget_available(month_start, estimated_cost_usd)

        candidate = await self._session.execute(
            text("""
                select status, proposed_name
                  from wb.discovery_candidates
                 where id = :candidate_id
                 for update
            """),
            {"candidate_id": candidate_id},
        )
        row = candidate.first()
        if row is None or row.status != "pending" or row.proposed_name is not None:
            await self._session.rollback()
            return None
        existing = await self._session.execute(
            text("""
                select exists (
                    select 1 from wb.ai_naming_requests where candidate_id = :candidate_id
                )
            """),
            {"candidate_id": candidate_id},
        )
        if existing.scalar_one():
            await self._session.rollback()
            return None

        request = await self._session.execute(
            text("""
                insert into wb.ai_cost_requests
                    (model, month_start, status, estimated_cost_usd, reserved_at)
                values (:model, :month_start, 'reserved', :estimated_cost_usd, :reserved_at)
                returning id
            """),
            {
                "model": model,
                "month_start": month_start,
                "estimated_cost_usd": estimated_cost_usd,
                "reserved_at": reserved_at,
            },
        )
        request_id = int(request.scalar_one())
        await self._session.execute(
            text("""
                insert into wb.ai_naming_requests (candidate_id, request_id, created_at)
                values (:candidate_id, :request_id, :created_at)
            """),
            {"candidate_id": candidate_id, "request_id": request_id, "created_at": reserved_at},
        )
        await self._session.commit()
        return request_id

    async def reserve_retry(
        self,
        *,
        model: str,
        feedback_ids: list[str],
        estimated_cost_usd: Decimal,
        reserved_at: dt.datetime,
        classifier_prompt_sha256: str | None = None,
        classifier_batch_size: int | None = None,
    ) -> int:
        """Reserve a one-time paid retry for claimed reviews with no labels.

        Original claims and cost requests remain untouched. One retry mapping
        per feedback id provides an auditable old/new request link.
        """
        if estimated_cost_usd < 0:
            raise ValueError("estimated cost cannot be negative")
        if not feedback_ids:
            raise ValueError("cannot reserve an empty review batch")
        if len(set(feedback_ids)) != len(feedback_ids):
            raise ValueError("review batch contains duplicate feedback ids")

        await self._session.execute(
            text("select pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _BUDGET_LOCK_KEY},
        )
        month_start = calendar_month_start(reserved_at)
        await self._ensure_budget_available(month_start, estimated_cost_usd)

        eligible = await self._session.execute(
            text("""
                select c.feedback_id, c.request_id
                  from unnest(cast(:ids as text[])) as wanted(feedback_id)
                  join wb.ai_review_claims c using (feedback_id)
                  join wb.ai_cost_requests original_request
                    on original_request.id = c.request_id
                   and original_request.status in ('settled', 'uncertain')
                 where not exists (
                           select 1 from wb.feedback_reasons fr
                            where fr.feedback_id = c.feedback_id
                       )
                   and not exists (
                           select 1 from wb.ai_review_retries r
                            where r.feedback_id = c.feedback_id
                       )
                 order by c.feedback_id
            """),
            {"ids": feedback_ids},
        )
        previous_requests = {str(row.feedback_id): int(row.request_id) for row in eligible.all()}
        if set(previous_requests) != set(feedback_ids):
            await self._session.rollback()
            raise RetryNotAllowedError(
                "retry requires eligible claimed reviews without labels and without a prior retry"
            )

        request = await self._session.execute(
            text("""
                insert into wb.ai_cost_requests
                    (model, month_start, status, estimated_cost_usd, reserved_at,
                     classifier_prompt_sha256, classifier_batch_size)
                values (:model, :month_start, 'reserved', :estimated_cost_usd, :reserved_at,
                        :classifier_prompt_sha256, :classifier_batch_size)
                returning id
            """),
            {
                "model": model,
                "month_start": month_start,
                "estimated_cost_usd": estimated_cost_usd,
                "reserved_at": reserved_at,
                "classifier_prompt_sha256": classifier_prompt_sha256,
                "classifier_batch_size": classifier_batch_size,
            },
        )
        request_id = int(request.scalar_one())
        await self._session.execute(
            text("""
                insert into wb.ai_review_retries
                    (feedback_id, previous_request_id, retry_request_id, attempted_at)
                values (:feedback_id, :previous_request_id, :retry_request_id, :attempted_at)
            """),
            [
                {
                    "feedback_id": feedback_id,
                    "previous_request_id": previous_request_id,
                    "retry_request_id": request_id,
                    "attempted_at": reserved_at,
                }
                for feedback_id, previous_request_id in previous_requests.items()
            ],
        )
        await self._session.commit()
        return request_id

    async def _ensure_budget_available(
        self,
        month_start: dt.date,
        estimated_cost_usd: Decimal,
    ) -> None:
        sentinel = await self._session.execute(
            text("""
                select exists (
                    select 1 from wb.ai_cost_requests
                     where month_start = :month_start
                       and status = 'uncertain' and consumes_monthly_cap
                )
            """),
            {"month_start": month_start},
        )
        if sentinel.scalar_one():
            await self._session.rollback()
            raise CostLimitExceededError(
                "AI monthly budget is blocked by an over-reservation uncertain request"
            )

        rows = await self._session.execute(
            text("""
                select coalesce(sum(case when status = 'settled' then actual_cost_usd
                                          else estimated_cost_usd end), 0)
                  from wb.ai_cost_requests where month_start = :month_start
            """),
            {"month_start": month_start},
        )
        already_spent = Decimal(str(rows.scalar_one()))
        if already_spent + estimated_cost_usd > MONTHLY_PROJECT_CAP_USD:
            await self._session.rollback()
            raise CostLimitExceededError(
                f"AI monthly project budget would exceed ${MONTHLY_PROJECT_CAP_USD}"
            )

    async def settle(
        self,
        request_id: int,
        *,
        input_tokens: int,
        output_tokens: int,
        actual_cost_usd: Decimal,
        settled_at: dt.datetime,
    ) -> None:
        await self._session.execute(
            text("""
                update wb.ai_cost_requests
                   set status = 'settled', actual_cost_usd = :actual_cost_usd,
                       input_tokens = :input_tokens, output_tokens = :output_tokens,
                       settled_at = :settled_at
                 where id = :request_id and status = 'reserved'
            """),
            {
                "request_id": request_id,
                "actual_cost_usd": actual_cost_usd,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "settled_at": settled_at,
            },
        )

    async def mark_uncertain(self, request_id: int, *, consume_cap: bool = False) -> None:
        """Keep the full reservation charged when outcome or usage is unknown."""
        await self._session.execute(
            text("""
                update wb.ai_cost_requests
                   set status = 'uncertain',
                       consumes_monthly_cap = :consume_cap,
                       estimated_cost_usd = case when :consume_cap then :cap
                                                 else estimated_cost_usd end
                 where id = :id and status = 'reserved'
            """),
            {"id": request_id, "consume_cap": consume_cap, "cap": MONTHLY_PROJECT_CAP_USD},
        )
        await self._session.commit()
