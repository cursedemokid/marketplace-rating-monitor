"""Paid one-shot naming proposals for pending discovery candidates."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from decimal import Decimal
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.llm.classifier import NameProposalResponse
from app.clock import Clock
from app.repositories.ai_cost import AiCostRepository
from app.repositories.ai_naming import AiNamingRepository
from app.services.ai_cost import ModelTariff, Usage, estimate_name_input_tokens
from app.services.classify import ClassificationError, sanitize


class NameProposer(Protocol):
    model: str

    async def propose_name(
        self, samples: Sequence[str], max_output_tokens: int
    ) -> NameProposalResponse: ...


async def propose_name(
    session: AsyncSession,
    candidate_id: int,
    classifier_or_client: NameProposer,
    tariff: ModelTariff | None,
    clock: Clock,
    *,
    max_input_tokens: int,
    max_output_tokens: int,
    sample_limit: int = 8,
    before_paid_request: Callable[[], Awaitable[None]] | None = None,
) -> str | None:
    """Return a pending candidate's proposed name, never approving the cause.

    Every candidate can be sent once. A durable reservation and candidate claim
    are committed before the API call; ambiguous outcomes are never retried.
    """
    if tariff is None or not isinstance(tariff, ModelTariff):
        raise ClassificationError("Для AI-названия не задан тариф; вызов запрещён")
    if tariff.model != classifier_or_client.model:
        raise ClassificationError("Тариф AI-названия не соответствует модели")
    if max_input_tokens < 1 or max_output_tokens < 1 or sample_limit < 1:
        raise ValueError("AI token limits and sample_limit must be positive")

    naming_repo = AiNamingRepository(session)
    samples = [
        sanitize(value).strip()
        for value in await naming_repo.sample_texts(candidate_id, limit=sample_limit)
    ]
    samples = [value for value in samples if value]
    if not samples:
        return None

    input_estimate = estimate_name_input_tokens(
        samples, model=classifier_or_client.model, max_output_tokens=max_output_tokens
    )
    if input_estimate > max_input_tokens:
        raise ClassificationError("Запрос названия превышает настроенный max_input_tokens")
    max_tokens = min(
        max_output_tokens, getattr(classifier_or_client, "max_output_tokens", max_output_tokens)
    )
    if before_paid_request is not None:
        await before_paid_request()
    reserved_cost = tariff.cost(input_estimate, max_tokens)
    cost_repo = AiCostRepository(session)
    request_id = await cost_repo.reserve_candidate_name(
        candidate_id=candidate_id,
        model=classifier_or_client.model,
        estimated_cost_usd=reserved_cost,
        reserved_at=clock.now(),
    )
    if request_id is None:
        return None

    try:
        response = await classifier_or_client.propose_name(samples, max_tokens)
        usage = getattr(response, "usage", None)
        actual_cost = getattr(response, "cost_usd", None)
        name = getattr(response, "name", None)
        if (
            not isinstance(response, NameProposalResponse)
            or not isinstance(usage, Usage)
            or not isinstance(actual_cost, Decimal)
            or actual_cost != tariff.cost(usage.input_tokens, usage.output_tokens)
        ):
            raise ClassificationError("Модель не предоставила проверяемые usage для названия")
        if usage.input_tokens > input_estimate or usage.output_tokens > max_tokens:
            await cost_repo.mark_uncertain(request_id, consume_cap=True)
            raise ClassificationError("usage названия превышает резерв; бюджет заблокирован")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ClassificationError("Модель вернула недопустимое название причины")
    except BaseException as exc:
        failed_usage = getattr(exc, "usage", None)
        failed_cost = getattr(exc, "cost_usd", None)
        if (
            isinstance(failed_usage, Usage)
            and isinstance(failed_cost, Decimal)
            and failed_cost == tariff.cost(failed_usage.input_tokens, failed_usage.output_tokens)
            and failed_usage.input_tokens <= input_estimate
            and failed_usage.output_tokens <= max_tokens
        ):
            await cost_repo.settle(
                request_id,
                input_tokens=failed_usage.input_tokens,
                output_tokens=failed_usage.output_tokens,
                actual_cost_usd=failed_cost,
                settled_at=clock.now(),
            )
            await session.commit()
        else:
            await cost_repo.mark_uncertain(request_id, consume_cap=isinstance(failed_usage, Usage))
        raise

    await cost_repo.settle(
        request_id,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        actual_cost_usd=actual_cost,
        settled_at=clock.now(),
    )
    await session.commit()
    clean_name = name.strip()
    if not await naming_repo.save_proposed_name(candidate_id, clean_name):
        await session.commit()
        return None
    await session.commit()
    return clean_name
