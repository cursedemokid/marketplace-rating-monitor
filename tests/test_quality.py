"""Micro metrics count each selected reason and exact review matches."""

import csv
import datetime as dt
from io import StringIO

import pytest
from app.repositories.feedbacks import FeedbackRepository, FeedbackRow
from app.repositories.products import ProductRepository, ProductRow
from app.services.quality import sample_csv, score_multilabel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def test_multilabel_quality_counts_overlapping_reasons() -> None:
    result = score_multilabel(
        [{1, 2}, {2}, {3}],
        [{1, 3}, {2}, {3}],
    )

    assert result.reviews == 3
    assert result.true_positive == 3
    assert result.false_positive == 1
    assert result.false_negative == 1
    assert result.precision == pytest.approx(0.75)
    assert result.recall == pytest.approx(0.75)
    assert result.f1 == pytest.approx(0.75)
    assert result.exact_match == pytest.approx(2 / 3)


async def test_quality_sample_excludes_existing_reference_ids(session: AsyncSession) -> None:
    await ProductRepository(session).upsert_many(
        [ProductRow(999_000_708, "QUALITY-TEST", None, None, None)]
    )
    await FeedbackRepository(session).upsert_many(
        [
            FeedbackRow(
                id=f"quality-{index}",
                nm_id=999_000_708,
                valuation=1,
                text="Плохо",
                pros="",
                cons="",
                created_at=dt.datetime(2026, 9, 24, tzinfo=dt.UTC),
                created_date=dt.date(2026, 9, 24),
            )
            for index in range(103)
        ]
    )
    reason_id = (
        await session.execute(text("select id from wb.negative_reasons order by id limit 1"))
    ).scalar_one()
    await session.execute(
        text("""
            insert into wb.feedback_reasons (feedback_id, reason_id, model, classified_at)
            values (:id, :reason, 'old-test-model', now())
        """),
        {"id": "quality-1", "reason": reason_id},
    )
    request_id = (
        await session.execute(
            text("""
                insert into wb.ai_cost_requests
                    (model, month_start, status, estimated_cost_usd, reserved_at)
                values ('old-test-model', 'DemoPhone', 'uncertain', 1, now())
                returning id
            """)
        )
    ).scalar_one()
    await session.execute(
        text("""
            insert into wb.ai_review_claims (feedback_id, request_id, claimed_at)
            values ('quality-2', :request_id, now())
        """),
        {"request_id": request_id},
    )

    content, _catalog = await sample_csv(
        session,
        count=100,
        exclude_ids={"quality-0"},
        unclassified_only=True,
    )
    ids = [row["feedback_id"] for row in csv.DictReader(StringIO(content))]

    assert len(ids) == 100
    assert "quality-0" not in ids
    assert "quality-1" not in ids
    assert "quality-2" not in ids
