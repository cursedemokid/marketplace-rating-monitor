"""Preflight must match the paid classifier's batch boundaries."""

from decimal import Decimal

from app.repositories.reasons import PendingReview
from app.services.ai_backlog import estimate_reserve
from app.services.ai_cost import ModelTariff


def test_preflight_excludes_blank_reviews_from_paid_reserve() -> None:
    tariff = ModelTariff("test-model", Decimal("1"), Decimal("1"))
    rows = [
        PendingReview("a", "  ", 1),
        PendingReview("b", "Плохо впитывают", 2),
        PendingReview("c", "Слишком дорого", 1),
    ]

    result = estimate_reserve(
        rows,
        {1: "пустой отзыв", 2: "протекают"},
        tariff=tariff,
        batch_size=2,
        max_output_tokens=4096,
    )

    assert result.attempted == 3
    assert result.local_blank == 1
    assert result.paid_reviews == 2
    assert result.paid_batches == 2
    assert result.max_cost_usd > 0


def test_preflight_all_blank_reviews_cost_nothing() -> None:
    result = estimate_reserve(
        [PendingReview("a", "", 1)],
        {1: "пустой отзыв"},
        tariff=ModelTariff("test-model", Decimal("1"), Decimal("1")),
        batch_size=30,
        max_output_tokens=4096,
    )
    assert result.max_cost_usd == 0
    assert result.paid_batches == 0


def test_preflight_reserves_configured_input_cap_and_rejects_oversized_estimate() -> None:
    tariff = ModelTariff("test-model", Decimal("0.1"), Decimal("0.2"))
    rows = [PendingReview("a", "Плохо впитывают", 2)]
    reasons = {1: "протекают"}

    result = estimate_reserve(
        rows,
        reasons,
        tariff=tariff,
        batch_size=1,
        max_output_tokens=100,
        max_input_tokens=32_768,
    )

    assert result.max_cost_usd == Decimal("0.0032968")

    import pytest

    with pytest.raises(ValueError, match="max_input_tokens"):
        estimate_reserve(
            [PendingReview("a", "x" * 100_000, 2)],
            reasons,
            tariff=tariff,
            batch_size=1,
            max_output_tokens=100,
            max_input_tokens=32_768,
        )
