"""Классификация: кэш по отзыву, очистка данных и валидация причин."""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Sequence
from decimal import Decimal

import httpx
import pytest
from app.clock import ManualClock
from app.repositories.feedbacks import FeedbackRepository, FeedbackRow
from app.repositories.products import ProductRepository, ProductRow
from app.repositories.reasons import ReasonRepository
from app.services.ai_cost import (
    CLASSIFIER_SYSTEM_PROMPT,
    ModelTariff,
    Usage,
    estimate_input_tokens,
)
from app.services.classify import ReviewInput
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def _feedback(session: AsyncSession, *, feedback_id: str, stars: int, body: str) -> None:
    await ProductRepository(session).upsert_many(
        [ProductRow(999_000_707, "CLASSIFY-TEST", None, None, None)]
    )
    await FeedbackRepository(session).upsert_many(
        [
            FeedbackRow(
                id=feedback_id,
                nm_id=999_000_707,
                valuation=stars,
                text=body,
                pros="",
                cons="",
                created_at=dt.datetime(2026, 9, 24, tzinfo=dt.UTC),
                created_date=dt.date(2026, 9, 24),
            )
        ]
    )


async def test_classification_is_cached_and_personal_data_is_redacted(
    session: AsyncSession,
    clock: ManualClock,
) -> None:
    from app.services.classify import run

    await _feedback(
        session,
        feedback_id="classify-negative",
        stars=2,
        body=(
            "Протекает. Фото https://example.org/photo.jpg, email DemoEmail, DemoPhone"
        ),
    )
    await ProductRepository(session).upsert_many(
        [
            ProductRow(
                999_000_707,
                "CLASSIFY-TEST",
                None,
                None,
                "6–10 кг",
                title="Подгузники https://catalog.example/current",
            )
        ]
    )
    await _feedback(session, feedback_id="classify-positive", stars=5, body="Отлично")
    reason_id = (
        await session.execute(text("select id from wb.negative_reasons where name = 'протекают'"))
    ).scalar_one()

    class Classifier:
        model = "test-model"
        tariff = ModelTariff("test-model", 1, 2)

        def __init__(self) -> None:
            self.calls = 0
            self.received: list[ReviewInput] = []

        async def classify(
            self, reviews: Sequence[ReviewInput], reasons: dict[int, str]
        ) -> dict[str, list[int]]:
            self.calls += 1
            self.received.extend(reviews)

            class Answer(dict[str, list[int]]):
                usage = Usage(10, 3)
                cost_usd = Decimal("0.000016")

            return Answer({"classify-negative": [reason_id]})

    classifier = Classifier()
    first = await run(
        session, classifier, clock, until=dt.date(2026, 9, 24), batch_size=30, max_stars=3
    )
    second = await run(
        session, classifier, clock, until=dt.date(2026, 9, 24), batch_size=30, max_stars=3
    )

    assert first.classified == 1 and second.classified == 0
    assert classifier.calls == 1
    sent = classifier.received[0]
    assert sent.feedback_id == "classify-negative"
    assert sent.stars == 2
    assert "Протекает" in sent.text
    assert "example.org" not in sent.text
    assert "me@" not in sent.text
    assert "999 123" not in sent.text
    assert sent.product_title == "Подгузники [ссылка]"
    assert sent.product_size == "6–10 кг"
    rows = (
        await session.execute(
            text("""
                select feedback_id, reason_id from wb.feedback_reasons
                 where feedback_id like 'classify-%'
            """)
        )
    ).all()
    assert rows == [("classify-negative", reason_id)]
    provenance = (
        await session.execute(
            text("""
                select r.classifier_prompt_sha256, r.classifier_batch_size
                  from wb.ai_review_claims c
                  join wb.ai_cost_requests r on r.id = c.request_id
                 where c.feedback_id = 'classify-negative'
            """)
        )
    ).one()
    assert (
        provenance.classifier_prompt_sha256
        == hashlib.sha256(CLASSIFIER_SYSTEM_PROMPT.encode("utf-8")).hexdigest()
    )
    assert provenance.classifier_batch_size == 30


async def test_overlong_current_product_metadata_is_rejected_before_paid_call(
    session: AsyncSession,
    clock: ManualClock,
) -> None:
    from app.services.classify import ClassificationError, run

    title = "Карточка " + "товар " * 10_000
    await _feedback(session, feedback_id="classify-long-product", stars=2, body="Маловаты")
    await ProductRepository(session).upsert_many(
        [
            ProductRow(
                999_000_707,
                "CLASSIFY-TEST",
                None,
                None,
                "6–10 кг",
                title=title,
            )
        ]
    )

    class Classifier:
        model = "test-model"
        tariff = ModelTariff("test-model", 1, 2)

        def __init__(self) -> None:
            self.calls = 0

        async def classify(
            self, reviews: Sequence[ReviewInput], reasons: dict[int, str]
        ) -> dict[str, list[int]]:
            self.calls += 1
            return {}

    classifier = Classifier()
    reasons = await ReasonRepository(session).approved()
    baseline_estimate = estimate_input_tokens(
        reasons,
        [("classify-long-product", "Маловаты", 2)],
        model="test-model",
        max_output_tokens=128,
    )
    product_estimate = estimate_input_tokens(
        reasons,
        [("classify-long-product", "Маловаты", 2)],
        model="test-model",
        max_output_tokens=128,
        product_context={"classify-long-product": (title, "6–10 кг")},
    )
    assert baseline_estimate < 32_768 < product_estimate
    with pytest.raises(ClassificationError, match="max_input_tokens"):
        await run(
            session,
            classifier,
            clock,
            until=dt.date(2026, 9, 24),
            batch_size=1,
            max_stars=3,
            max_input_tokens=32_768,
            feedback_ids=["classify-long-product"],
        )

    assert classifier.calls == 0


async def test_tariff_is_checked_before_each_paid_classification_batch(
    session: AsyncSession,
    clock: ManualClock,
) -> None:
    from app.services.classify import run

    ids = ["classify-tariff-a", "classify-tariff-b"]
    for feedback_id in ids:
        await _feedback(session, feedback_id=feedback_id, stars=1, body="Протекают")
    reason_id = (
        await session.execute(text("select id from wb.negative_reasons where name = 'протекают'"))
    ).scalar_one()
    checks = 0

    async def check_tariff() -> None:
        nonlocal checks
        checks += 1

    class Classifier:
        model = "test-model"
        tariff = ModelTariff("test-model", 1, 2)

        async def classify(
            self, reviews: Sequence[ReviewInput], reasons: dict[int, str]
        ) -> dict[str, list[int]]:
            class Answer(dict[str, list[int]]):
                usage = Usage(10, 3)
                cost_usd = Decimal("0.000016")

            return Answer({reviews[0].feedback_id: [reason_id]})

    result = await run(
        session,
        Classifier(),
        clock,
        until=dt.date(2026, 9, 24),
        batch_size=1,
        max_stars=3,
        limit=2,
        feedback_ids=ids,
        before_paid_request=check_tariff,
    )

    assert result.classified == 2
    assert checks == 2


async def test_changed_tariff_does_not_claim_unsent_review(
    session: AsyncSession,
    clock: ManualClock,
) -> None:
    from app.services.classify import run

    await _feedback(session, feedback_id="classify-tariff-blocked", stars=1, body="Плохо")

    class Classifier:
        model = "test-model"
        tariff = ModelTariff("test-model", 1, 2)

        async def classify(
            self, reviews: Sequence[ReviewInput], reasons: dict[int, str]
        ) -> dict[str, list[int]]:
            pytest.fail("provider request must not happen after tariff change")

    async def reject_tariff() -> None:
        raise RuntimeError("tariff changed")

    with pytest.raises(RuntimeError, match="tariff changed"):
        await run(
            session,
            Classifier(),
            clock,
            until=dt.date(2026, 9, 24),
            batch_size=1,
            max_stars=3,
            limit=1,
            feedback_ids=["classify-tariff-blocked"],
            before_paid_request=reject_tariff,
        )

    claimed = (
        await session.execute(
            text(
                "select count(*) from wb.ai_review_claims "
                "where feedback_id = 'classify-tariff-blocked'"
            )
        )
    ).scalar_one()
    assert claimed == 0


async def test_unknown_reason_is_not_saved(session: AsyncSession, clock: ManualClock) -> None:
    from app.services.classify import run

    await _feedback(session, feedback_id="classify-unknown", stars=1, body="Плохо")

    class Classifier:
        model = "test-model"
        tariff = ModelTariff("test-model", 1, 2)

        async def classify(
            self, reviews: Sequence[ReviewInput], reasons: dict[int, str]
        ) -> dict[str, list[int]]:
            class Answer(dict[str, list[int]]):
                usage = Usage(10, 3)
                cost_usd = Decimal("0.000016")

            return Answer({"classify-unknown": [999_999]})

    result = await run(
        session, Classifier(), clock, until=dt.date(2026, 9, 24), batch_size=30, max_stars=3
    )

    assert result.failed == 1
    assert result.attempted == 1

    count = (
        await session.execute(
            text("select count(*) from wb.feedback_reasons where feedback_id = 'classify-unknown'")
        )
    ).scalar_one()
    assert count == 0


async def test_quality_sample_selection_only_returns_requested_reviews(
    session: AsyncSession,
) -> None:
    await _feedback(session, feedback_id="sample-include", stars=2, body="Плохо")
    await _feedback(session, feedback_id="sample-exclude", stars=1, body="Ужасно")

    rows = await ReasonRepository(session).pending(
        until=dt.date(2026, 9, 24),
        max_stars=3,
        limit=10,
        feedback_ids=["sample-include"],
    )

    assert [row.feedback_id for row in rows] == ["sample-include"]


async def test_blank_review_gets_local_empty_review_reason_without_provider_call(
    session: AsyncSession, clock: ManualClock
) -> None:
    from app.services.classify import run

    await _feedback(session, feedback_id="classify-empty", stars=1, body="   ")
    empty_reason = (
        await session.execute(
            text("select id from wb.negative_reasons where name = 'пустой отзыв'")
        )
    ).scalar_one()

    class Classifier:
        model = "test-model"
        tariff = ModelTariff("test-model", 1, 2)

        async def classify(self, reviews, reasons):
            pytest.fail("blank review must not be sent to the provider")

    result = await run(
        session, Classifier(), clock, until=dt.date(2026, 9, 24), batch_size=1, max_stars=3
    )

    assert result.classified == 1
    assert result.attempted == 1
    assert result.failed == 0
    saved = (
        await session.execute(
            text("select reason_id from wb.feedback_reasons where feedback_id = 'classify-empty'")
        )
    ).scalar_one()
    assert saved == empty_reason
    calls = (
        await session.execute(
            text("select count(*) from wb.ai_cost_requests where model = 'test-model'")
        )
    ).scalar_one()
    assert calls == 0


async def test_transport_failure_keeps_full_reserve_and_continues_to_next_review(
    session: AsyncSession, clock: ManualClock
) -> None:
    from app.services.classify import run

    for feedback_id in ("classify-a-failure", "classify-b-success"):
        await _feedback(session, feedback_id=feedback_id, stars=2, body="Протекает")
    reason_id = (
        await session.execute(text("select id from wb.negative_reasons where name = 'протекают'"))
    ).scalar_one()
    calls: list[str] = []

    class Classifier:
        model = "test-model"
        tariff = ModelTariff("test-model", 1, 2)

        async def classify(self, reviews, reasons):
            calls.append(reviews[0].feedback_id)
            if reviews[0].feedback_id == "classify-a-failure":
                raise httpx.ConnectError("temporary outage")

            class Answer(dict):
                usage = Usage(10, 3)
                cost_usd = Decimal("0.000016")

            return Answer({reviews[0].feedback_id: [reason_id]})

    result = await run(
        session, Classifier(), clock, until=dt.date(2026, 9, 24), batch_size=1, max_stars=3
    )

    assert calls == ["classify-a-failure", "classify-b-success"]
    assert result.attempted == 2
    assert result.classified == 1
    assert result.failed == 1
    failed_cost = (
        await session.execute(
            text("select status, estimated_cost_usd from wb.ai_cost_requests order by id limit 1")
        )
    ).one()
    assert failed_cost.status == "uncertain"
    assert Decimal(str(failed_cost.estimated_cost_usd)) > 0
    claims = (
        await session.execute(
            text(
                "select count(*) from wb.ai_review_claims where feedback_id = 'classify-a-failure'"
            )
        )
    ).scalar_one()
    assert claims == 1


async def test_three_consecutive_provider_failures_stop_run_and_report_deferred(
    session: AsyncSession, clock: ManualClock
) -> None:
    from app.services.classify import run

    ids = [f"classify-streak-{i}" for i in range(4)]
    for feedback_id in ids:
        await _feedback(session, feedback_id=feedback_id, stars=2, body="Плохо")
    calls = 0

    class Classifier:
        model = "test-model"
        tariff = ModelTariff("test-model", 1, 2)

        async def classify(self, reviews, reasons):
            nonlocal calls
            calls += 1
            raise httpx.ConnectError("temporary outage")

    result = await run(
        session,
        Classifier(),
        clock,
        until=dt.date(2026, 9, 24),
        batch_size=1,
        max_stars=3,
    )

    assert calls == 3
    assert result.attempted == 3
    assert result.failed == 3
    assert result.deferred == 1


async def test_usage_above_reservation_stops_paid_classification(
    session: AsyncSession, clock: ManualClock
) -> None:
    from app.services.classify import ClassificationError, run

    await _feedback(session, feedback_id="classify-over-reserve", stars=1, body="Плохо")
    reason_id = (
        await session.execute(text("select id from wb.negative_reasons where name = 'протекают'"))
    ).scalar_one()

    class Classifier:
        model = "test-model"
        tariff = ModelTariff("test-model", 1, 2)

        async def classify(self, reviews, reasons):
            usage = Usage(1_000_000, 3)

            class Answer(dict):
                pass

            result = Answer({reviews[0].feedback_id: [reason_id]})
            result.usage = usage
            result.cost_usd = self.tariff.cost(usage.input_tokens, usage.output_tokens)
            return result

    with pytest.raises(ClassificationError, match="usage превышает"):
        await run(
            session,
            Classifier(),
            clock,
            until=dt.date(2026, 9, 24),
            batch_size=1,
            max_stars=3,
            feedback_ids=["classify-over-reserve"],
        )


async def test_explicit_retry_uses_one_time_retry_reservation(
    session: AsyncSession, clock: ManualClock
) -> None:
    from app.repositories.ai_cost import AiCostRepository
    from app.services.classify import run

    feedback_id = "classify-retry-once"
    await _feedback(session, feedback_id=feedback_id, stars=1, body="Плохо")
    cost_repo = AiCostRepository(session)
    original_request = await cost_repo.reserve(
        model="test-model",
        feedback_ids=[feedback_id],
        estimated_cost_usd=Decimal("0.01"),
        reserved_at=clock.now(),
    )
    await cost_repo.mark_uncertain(original_request)
    reason_id = (
        await session.execute(text("select id from wb.negative_reasons where name = 'протекают'"))
    ).scalar_one()

    class Classifier:
        model = "test-model"
        tariff = ModelTariff("test-model", 1, 2)

        async def classify(self, reviews, reasons):
            class Answer(dict):
                usage = Usage(10, 3)
                cost_usd = Decimal("0.000016")

            return Answer({reviews[0].feedback_id: [reason_id]})

    result = await run(
        session,
        Classifier(),
        clock,
        until=dt.date(2026, 9, 24),
        batch_size=1,
        max_stars=3,
        retry_feedback_ids=[feedback_id],
    )

    assert result.classified == 1
    assert result.attempted == 1
    retry = (
        await session.execute(
            text("select previous_request_id, retry_request_id from wb.ai_review_retries")
        )
    ).one()
    assert retry.previous_request_id == original_request
    assert retry.retry_request_id != original_request


async def test_auth_http_error_is_fatal_and_keeps_review_claimed(
    session: AsyncSession, clock: ManualClock
) -> None:
    from app.services.classify import run

    feedback_id = "classify-http-auth"
    await _feedback(session, feedback_id=feedback_id, stars=1, body="Плохо")

    class Classifier:
        model = "test-model"
        tariff = ModelTariff("test-model", 1, 2)

        async def classify(self, reviews, reasons):
            request = httpx.Request("POST", "https://internal.example.invalid/endpoint")
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

    with pytest.raises(httpx.HTTPStatusError):
        await run(
            session,
            Classifier(),
            clock,
            until=dt.date(2026, 9, 24),
            batch_size=1,
            max_stars=3,
            feedback_ids=[feedback_id],
        )

    status = (
        await session.execute(
            text("select status from wb.ai_cost_requests where model = 'test-model'")
        )
    ).scalar_one()
    assert status == "uncertain"
    claim_count = (
        await session.execute(
            text("select count(*) from wb.ai_review_claims where feedback_id = :feedback_id"),
            {"feedback_id": feedback_id},
        )
    ).scalar_one()
    assert claim_count == 1


async def test_configured_input_cap_reserves_full_cost_and_accepts_larger_provider_usage(
    session: AsyncSession, clock: ManualClock
) -> None:
    from app.services.classify import run

    feedback_id = "classify-configured-input-cap"
    await _feedback(session, feedback_id=feedback_id, stars=1, body="Плохо")
    reason_id = (
        await session.execute(text("select id from wb.negative_reasons where name = 'протекают'"))
    ).scalar_one()
    test_tariff = ModelTariff("test-model", Decimal("0.1"), Decimal("0.2"))

    class Classifier:
        model = "test-model"
        tariff = test_tariff

        async def classify(self, reviews, reasons):
            reservation = (
                await session.execute(
                    text(
                        "select status, estimated_cost_usd from wb.ai_cost_requests "
                        "order by id desc limit 1"
                    )
                )
            ).one()
            assert reservation.status == "reserved"
            assert Decimal(str(reservation.estimated_cost_usd)) == Decimal("0.0033024")

            class Answer(dict):
                usage = Usage(30_146, 3)
                cost_usd = test_tariff.cost(30_146, 3)

            return Answer({reviews[0].feedback_id: [reason_id]})

    result = await run(
        session,
        Classifier(),
        clock,
        until=dt.date(2026, 9, 24),
        batch_size=1,
        max_stars=3,
        max_input_tokens=32_768,
    )

    assert result.classified == 1
    settled = (
        await session.execute(
            text("select status, input_tokens from wb.ai_cost_requests order by id desc limit 1")
        )
    ).one()
    assert settled.status == "settled"
    assert settled.input_tokens == 30_146


async def test_configured_input_cap_blocks_usage_above_cap(
    session: AsyncSession, clock: ManualClock
) -> None:
    from app.services.classify import ClassificationError, run

    feedback_id = "classify-configured-input-over-cap"
    await _feedback(session, feedback_id=feedback_id, stars=1, body="Плохо")
    reason_id = (
        await session.execute(text("select id from wb.negative_reasons where name = 'протекают'"))
    ).scalar_one()

    class Classifier:
        model = "test-model"
        tariff = ModelTariff("test-model", 1, 2)

        async def classify(self, reviews, reasons):
            usage = Usage(32_769, 3)

            class Answer(dict):
                pass

            answer = Answer({reviews[0].feedback_id: [reason_id]})
            answer.usage = usage
            answer.cost_usd = self.tariff.cost(usage.input_tokens, usage.output_tokens)
            return answer

    with pytest.raises(ClassificationError, match="usage превышает"):
        await run(
            session,
            Classifier(),
            clock,
            until=dt.date(2026, 9, 24),
            batch_size=1,
            max_stars=3,
            max_input_tokens=32_768,
        )


async def test_configured_input_cap_settles_usage_attached_to_provider_error(
    session: AsyncSession, clock: ManualClock
) -> None:
    from app.services.classify import run

    await _feedback(
        session, feedback_id="classify-configured-input-error-usage", stars=1, body="Плохо"
    )

    class Classifier:
        model = "test-model"
        tariff = ModelTariff("test-model", 1, 2)

        async def classify(self, reviews, reasons):
            error = httpx.ConnectError("connection interrupted after response")
            error.usage = Usage(30_146, 3)
            error.cost_usd = self.tariff.cost(30_146, 3)
            raise error

    result = await run(
        session,
        Classifier(),
        clock,
        until=dt.date(2026, 9, 24),
        batch_size=1,
        max_stars=3,
        max_input_tokens=32_768,
    )

    assert result.failed == 1
    request = (
        await session.execute(
            text("select status, input_tokens from wb.ai_cost_requests order by id desc limit 1")
        )
    ).one()
    assert request.status == "settled"
    assert request.input_tokens == 30_146
