"""Read-only estimate of the maximum project charge for a bounded AI run."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.repositories.reasons import PendingReview
from app.services.ai_cost import ModelTariff, completion_token_limit, estimate_input_tokens
from app.services.classify import sanitize


@dataclass(frozen=True, slots=True)
class ReserveEstimate:
    attempted: int
    local_blank: int
    paid_reviews: int
    paid_batches: int
    max_cost_usd: Decimal


def estimate_reserve(
    rows: Sequence[PendingReview],
    reasons: dict[int, str],
    *,
    tariff: ModelTariff,
    batch_size: int,
    max_output_tokens: int,
    max_input_tokens: int | None = None,
) -> ReserveEstimate:
    if (
        batch_size < 1
        or max_output_tokens < 1
        or (max_input_tokens is not None and max_input_tokens < 1)
    ):
        raise ValueError("AI batch and token limits must be positive")
    blank = paid = batches = 0
    maximum = Decimal("0")
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        text_rows = [row for row in batch if row.body.strip()]
        blank += len(batch) - len(text_rows)
        if not text_rows:
            continue
        reviews = [(row.feedback_id, sanitize(row.body), row.stars) for row in text_rows]
        output_limit = min(completion_token_limit(len(reviews)), max_output_tokens)
        input_limit = estimate_input_tokens(
            reasons,
            reviews,
            model=tariff.model,
            max_output_tokens=output_limit,
            product_context={
                row.feedback_id: (row.product_title, row.product_size) for row in text_rows
            },
        )
        if max_input_tokens is not None and input_limit > max_input_tokens:
            raise ValueError("Запрос превышает настроенный max_input_tokens")
        maximum += tariff.cost(
            max_input_tokens if max_input_tokens is not None else input_limit,
            output_limit,
        )
        paid += len(text_rows)
        batches += 1
    return ReserveEstimate(len(rows), blank, paid, batches, maximum)
