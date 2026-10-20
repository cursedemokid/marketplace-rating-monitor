"""The monthly project cap is serialized across concurrent PostgreSQL sessions."""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from decimal import Decimal
from uuid import uuid4

import pytest
from app.db.engine import build_engine
from app.db.session import build_session_factory
from app.repositories.ai_cost import (
    AiCostRepository,
    CostLimitExceededError,
    RetryNotAllowedError,
)
from sqlalchemy import text


async def test_parallel_reservations_cannot_exceed_monthly_cap() -> None:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("нет DATABASE_URL")
    engine = build_engine(url)
    factory = build_session_factory(engine)
    suffix = uuid4().hex
    product_id = 999_200_000 + uuid4().int % 500_000
    review_ids = [f"budget-{suffix}-a", f"budget-{suffix}-b"]
    month = dt.date(2099, 5, 1)
    try:
        async with factory() as session:
            await session.execute(
                text("""
                insert into wb.products (nm_id, vendor_code, updated_at)
                values (:nm_id, :vendor_code, now())
            """),
                {"nm_id": product_id, "vendor_code": f"BUDGET-{suffix}"},
            )
            await session.execute(
                text("""
                insert into wb.feedbacks
                    (id, nm_id, valuation, created_at, created_date, imported_at)
                values (:id, :nm_id, 1, now(), :created_date, now())
            """),
                [
                    {"id": review_id, "nm_id": product_id, "created_date": month}
                    for review_id in review_ids
                ],
            )
            await session.commit()

        async def reserve(review_id: str) -> int | None:
            async with factory() as session:
                try:
                    return await AiCostRepository(session).reserve(
                        model="test-concurrency",
                        feedback_ids=[review_id],
                        estimated_cost_usd=Decimal("60.00"),
                        reserved_at=dt.datetime(2099, 5, 1, 12, tzinfo=dt.UTC),
                    )
                except CostLimitExceededError:
                    return None

        results = await asyncio.gather(*(reserve(review_id) for review_id in review_ids))
        assert sum(result is not None for result in results) == 1
        async with factory() as session:
            recorded = await session.execute(
                text("""
                select coalesce(sum(estimated_cost_usd), 0)
                  from wb.ai_cost_requests
                 where month_start = :month and model = 'test-concurrency'
            """),
                {"month": month},
            )
            assert recorded.scalar_one() == Decimal("60.00")
    finally:
        async with factory() as session:
            await session.execute(
                text("""
                delete from wb.ai_review_claims where feedback_id = any(cast(:ids as text[]))
            """),
                {"ids": review_ids},
            )
            await session.execute(
                text("""
                delete from wb.ai_cost_requests
                 where model = 'test-concurrency' and month_start = :month
            """),
                {"month": month},
            )
            await session.execute(
                text("""
                delete from wb.feedbacks where id = any(cast(:ids as text[]))
            """),
                {"ids": review_ids},
            )
            await session.execute(
                text("delete from wb.products where nm_id = :nm_id"), {"nm_id": product_id}
            )
            await session.commit()
        await engine.dispose()


async def test_bounded_uncertain_allows_automatic_reservation_and_keeps_audit_claims() -> None:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("нет DATABASE_URL")
    engine = build_engine(url)
    factory = build_session_factory(engine)
    suffix = uuid4().hex
    product_id = 999_200_000 + uuid4().int % 500_000
    original_id, fresh_id, manual_id = (
        f"uncertain-{suffix}-a",
        f"uncertain-{suffix}-b",
        f"uncertain-{suffix}-c",
    )
    month = dt.date(2099, 6, 1)
    first_request_id = second_request_id = manual_request_id = None
    try:
        async with factory() as session:
            await session.execute(
                text(
                    "insert into wb.products (nm_id, vendor_code, updated_at) "
                    "values (:nm_id, :code, now())"
                ),
                {"nm_id": product_id, "code": f"UNCERTAIN-{suffix}"},
            )
            await session.execute(
                text(
                    "insert into wb.feedbacks "
                    "(id, nm_id, valuation, created_at, created_date, imported_at) "
                    "values (:id, :nm_id, 1, now(), :created_date, now())"
                ),
                [
                    {"id": review_id, "nm_id": product_id, "created_date": month}
                    for review_id in (original_id, fresh_id, manual_id)
                ],
            )
            await session.commit()

        async with factory() as session:
            repo = AiCostRepository(session)
            first_request_id = await repo.reserve(
                model="test-manual-uncertain",
                feedback_ids=[original_id],
                estimated_cost_usd=Decimal("0.005368"),
                reserved_at=dt.datetime(2099, 6, 1, 12, tzinfo=dt.UTC),
            )
            await repo.mark_uncertain(first_request_id)

        async with factory() as session:
            second_request_id = await AiCostRepository(session).reserve(
                model="test-manual-uncertain",
                feedback_ids=[fresh_id],
                estimated_cost_usd=Decimal("0.01"),
                reserved_at=dt.datetime(2099, 6, 1, 13, tzinfo=dt.UTC),
            )

        async with factory() as session:
            manual_request_id = await AiCostRepository(session).reserve(
                model="test-manual-uncertain",
                feedback_ids=[manual_id],
                estimated_cost_usd=Decimal("0.01"),
                reserved_at=dt.datetime(2099, 6, 1, 14, tzinfo=dt.UTC),
                allow_uncertain_manual=True,
            )

        async with factory() as session:
            original = (
                await session.execute(
                    text(
                        "select status, estimated_cost_usd, actual_cost_usd "
                        "from wb.ai_cost_requests where id = :id"
                    ),
                    {"id": first_request_id},
                )
            ).one()
            audit = (
                await session.execute(
                    text(
                        "select uncertain_request_id from wb.ai_uncertain_admissions "
                        "where request_id = :id"
                    ),
                    {"id": manual_request_id},
                )
            ).scalar_one()
            claims = (
                (
                    await session.execute(
                        text(
                            "select feedback_id from wb.ai_review_claims "
                            "where request_id in (:original, :fresh, :manual)"
                        ),
                        {
                            "original": first_request_id,
                            "fresh": second_request_id,
                            "manual": manual_request_id,
                        },
                    )
                )
                .scalars()
                .all()
            )
            assert tuple(original) == ("uncertain", Decimal("0.005368"), None)
            assert audit == first_request_id
            assert set(claims) == {original_id, fresh_id, manual_id}
    finally:
        async with factory() as session:
            await session.execute(
                text(
                    "delete from wb.ai_uncertain_admissions "
                    "where request_id in (:fresh, :manual) or uncertain_request_id = :original"
                ),
                {
                    "fresh": second_request_id,
                    "manual": manual_request_id,
                    "original": first_request_id,
                },
            )
            await session.execute(
                text(
                    "delete from wb.ai_review_claims "
                    "where feedback_id in (:original, :fresh, :manual)"
                ),
                {"original": original_id, "fresh": fresh_id, "manual": manual_id},
            )
            await session.execute(
                text("delete from wb.ai_cost_requests where id in (:original, :fresh, :manual)"),
                {
                    "original": first_request_id,
                    "fresh": second_request_id,
                    "manual": manual_request_id,
                },
            )
            await session.execute(
                text("delete from wb.feedbacks where id in (:original, :fresh, :manual)"),
                {"original": original_id, "fresh": fresh_id, "manual": manual_id},
            )
            await session.execute(
                text("delete from wb.products where nm_id = :id"), {"id": product_id}
            )
            await session.commit()
        await engine.dispose()


async def test_retry_is_one_time_and_preserves_claim_under_concurrency() -> None:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("нет DATABASE_URL")
    engine = build_engine(url)
    factory = build_session_factory(engine)
    suffix = uuid4().hex
    product_id = 999_200_000 + uuid4().int % 500_000
    review_id = f"retry-{suffix}"
    month = dt.date(2099, 7, 1)
    original_request_id = retry_request_id = None
    try:
        async with factory() as session:
            await session.execute(
                text(
                    "insert into wb.products (nm_id, vendor_code, updated_at) "
                    "values (:nm_id, :code, now())"
                ),
                {"nm_id": product_id, "code": f"RETRY-{suffix}"},
            )
            await session.execute(
                text(
                    "insert into wb.feedbacks "
                    "(id, nm_id, valuation, created_at, created_date, imported_at) "
                    "values (:id, :nm_id, 1, now(), :created_date, now())"
                ),
                {"id": review_id, "nm_id": product_id, "created_date": month},
            )
            await session.commit()

        async with factory() as session:
            original_request_id = await AiCostRepository(session).reserve(
                model="test-retry",
                feedback_ids=[review_id],
                estimated_cost_usd=Decimal("0.02"),
                reserved_at=dt.datetime(2099, 7, 1, 12, tzinfo=dt.UTC),
            )
            await AiCostRepository(session).mark_uncertain(original_request_id)

        async def reserve_retry() -> int | BaseException:
            async with factory() as session:
                try:
                    return await AiCostRepository(session).reserve_retry(
                        model="test-retry",
                        feedback_ids=[review_id],
                        estimated_cost_usd=Decimal("0.02"),
                        reserved_at=dt.datetime(2099, 7, 1, 13, tzinfo=dt.UTC),
                    )
                except RetryNotAllowedError as exc:
                    return exc

        outcomes = await asyncio.gather(reserve_retry(), reserve_retry())
        successful = [value for value in outcomes if isinstance(value, int)]
        rejected = [value for value in outcomes if isinstance(value, RetryNotAllowedError)]
        assert len(successful) == len(rejected) == 1
        retry_request_id = successful[0]

        # A failed provider call leaves the retry link in place for audit.
        async with factory() as session:
            await AiCostRepository(session).mark_uncertain(retry_request_id)

        async with factory() as session:
            original_claim = await session.execute(
                text("select request_id from wb.ai_review_claims where feedback_id = :id"),
                {"id": review_id},
            )
            retry_link = await session.execute(
                text(
                    "select previous_request_id, retry_request_id "
                    "from wb.ai_review_retries where feedback_id = :id"
                ),
                {"id": review_id},
            )
            retry_status = await session.execute(
                text("select status from wb.ai_cost_requests where id = :id"),
                {"id": retry_request_id},
            )
            assert original_claim.scalar_one() == original_request_id
            assert tuple(retry_link.one()) == (original_request_id, retry_request_id)
            assert retry_status.scalar_one() == "uncertain"
    finally:
        async with factory() as session:
            await session.execute(
                text("delete from wb.ai_review_retries where feedback_id = :id"),
                {"id": review_id},
            )
            await session.execute(
                text("delete from wb.ai_review_claims where feedback_id = :id"),
                {"id": review_id},
            )
            await session.execute(
                text("delete from wb.ai_cost_requests where id in (:original, :retry)"),
                {"original": original_request_id, "retry": retry_request_id},
            )
            await session.execute(
                text("delete from wb.feedbacks where id = :id"), {"id": review_id}
            )
            await session.execute(
                text("delete from wb.products where nm_id = :id"), {"id": product_id}
            )
            await session.commit()
        await engine.dispose()
