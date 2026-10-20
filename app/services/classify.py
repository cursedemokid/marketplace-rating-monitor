"""Классификация негативных отзывов с кэшем в PostgreSQL."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

import httpx
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import Clock
from app.repositories.ai_cost import AiCostRepository
from app.repositories.reasons import ReasonRepository
from app.services.ai_cost import (
    CLASSIFIER_SYSTEM_PROMPT,
    ModelTariff,
    Usage,
    completion_token_limit,
    estimate_input_tokens,
)

_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE = re.compile(
    r"(?<!\w)(?:\+7|8)[\s()\-]*\d{3}[\s()\-]*\d{3}[\s()\-]*\d{2}[\s()\-]*\d{2}(?!\w)"
)


class ClassificationError(ValueError):
    """Модель вернула неполный или запрещённый результат."""


class UnsafeUsageError(ClassificationError):
    """Provider usage is malformed or exceeds the reservation; stop paid calls."""


@dataclass(frozen=True, slots=True)
class ReviewInput:
    feedback_id: str
    text: str
    stars: int
    product_title: str = ""
    product_size: str = ""


class ReasonClassifier(Protocol):
    model: str
    tariff: ModelTariff

    async def classify(
        self, reviews: Sequence[ReviewInput], reasons: dict[int, str]
    ) -> dict[str, list[int]]:
        """Коды утверждённых причин для каждого отзыва."""


@dataclass(frozen=True, slots=True)
class ClassifyResult:
    classified: int
    batches: int
    attempted: int = 0
    failed: int = 0
    deferred: int = 0
    stopped_due_provider: bool = False


def sanitize(text: str) -> str:
    """Удалить контакты и ссылки, которые могли попасть в свободный текст."""
    text = _URL.sub("[ссылка]", text)
    text = _EMAIL.sub("[почта]", text)
    return _PHONE.sub("[телефон]", text)


async def run(
    session: AsyncSession,
    classifier: ReasonClassifier,
    clock: Clock,
    *,
    until: dt.date,
    batch_size: int,
    max_stars: int,
    limit: int | None = None,
    feedback_ids: list[str] | None = None,
    max_input_tokens: int | None = None,
    max_output_tokens: int | None = None,
    allow_uncertain_manual: bool = False,
    retry_feedback_ids: list[str] | None = None,
    before_paid_request: Callable[[], Awaitable[None]] | None = None,
) -> ClassifyResult:
    """Classify at most `limit` reviews (None means all) under the project cost cap."""
    if batch_size < 1 or (limit is not None and limit < 0):
        raise ValueError("batch_size must be positive and limit non-negative")
    repo = ReasonRepository(session)
    reasons = await repo.approved()
    fallback = next(
        (reason_id for reason_id, name in reasons.items() if name == "причина не указана"),
        None,
    )
    if fallback is None:
        raise ClassificationError("В справочнике нет причины «причина не указана»")

    empty_reason = next(
        (reason_id for reason_id, name in reasons.items() if name == "пустой отзыв"), None
    )
    if empty_reason is None:
        raise ClassificationError("В справочнике нет утверждённой причины «пустой отзыв»")

    cost_repo = AiCostRepository(session)
    classified = batches = attempted = failed = 0
    consecutive_provider_failures = 0
    while limit is None or attempted < limit:
        fetch_limit = batch_size if limit is None else min(batch_size, limit - attempted)
        if retry_feedback_ids is not None:
            pending = await repo.pending_retry(
                until=until,
                max_stars=max_stars,
                limit=fetch_limit,
                feedback_ids=retry_feedback_ids,
            )
        else:
            pending = await repo.pending(
                until=until,
                max_stars=max_stars,
                limit=fetch_limit,
                feedback_ids=feedback_ids,
            )
        if not pending:
            break
        blank = [row for row in pending if not sanitize(row.body).strip()]
        if blank:
            await repo.save(
                [(row.feedback_id, empty_reason) for row in blank],
                model="local:empty-review",
                classified_at=clock.now(),
            )
            await session.commit()
            classified += len(blank)
            attempted += len(blank)
        pending = [row for row in pending if sanitize(row.body).strip()]
        if not pending:
            continue
        reviews = [
            ReviewInput(
                row.feedback_id,
                sanitize(row.body),
                row.stars,
                product_title=sanitize(row.product_title),
                product_size=sanitize(row.product_size),
            )
            for row in pending
        ]
        tariff = getattr(classifier, "tariff", None)
        if not isinstance(tariff, ModelTariff) or tariff.model != classifier.model:
            raise ClassificationError("Для модели не задан тариф; платная классификация запрещена")
        if (
            max_output_tokens is not None
            and getattr(classifier, "max_output_tokens", max_output_tokens) > max_output_tokens
        ):
            raise ClassificationError(
                "max_output_tokens сервиса не может быть ниже лимита HTTP-классификатора"
            )
        max_tokens = min(
            completion_token_limit(len(reviews)),
            max_output_tokens if max_output_tokens is not None else 4096,
        )
        input_estimate = estimate_input_tokens(
            reasons,
            [(review.feedback_id, review.text, review.stars) for review in reviews],
            model=classifier.model,
            max_output_tokens=max_tokens,
            product_context={
                review.feedback_id: (review.product_title, review.product_size)
                for review in reviews
            },
        )
        if max_tokens < 1 or (max_input_tokens is not None and max_input_tokens < 1):
            raise ValueError("AI token limits must be positive")
        if max_input_tokens is not None and input_estimate > max_input_tokens:
            raise ClassificationError("Запрос превышает настроенный max_input_tokens")
        if before_paid_request is not None:
            await before_paid_request()
        reserved_input_tokens = max_input_tokens if max_input_tokens is not None else input_estimate
        reserved_cost = tariff.cost(reserved_input_tokens, max_tokens)
        batch_ids = [review.feedback_id for review in reviews]
        reserved_at = clock.now()
        prompt_sha256 = hashlib.sha256(CLASSIFIER_SYSTEM_PROMPT.encode("utf-8")).hexdigest()
        if retry_feedback_ids is not None:
            reservation_id = await cost_repo.reserve_retry(
                model=classifier.model,
                feedback_ids=batch_ids,
                estimated_cost_usd=reserved_cost,
                reserved_at=reserved_at,
                classifier_prompt_sha256=prompt_sha256,
                classifier_batch_size=batch_size,
            )
        else:
            reservation_id = await cost_repo.reserve(
                model=classifier.model,
                feedback_ids=batch_ids,
                estimated_cost_usd=reserved_cost,
                reserved_at=reserved_at,
                allow_uncertain_manual=allow_uncertain_manual,
                classifier_prompt_sha256=prompt_sha256,
                classifier_batch_size=batch_size,
            )
        attempted += len(reviews)
        batches += 1
        try:
            answer = await classifier.classify(reviews, reasons)
            usage = getattr(answer, "usage", None)
            actual_cost = getattr(answer, "cost_usd", None)
            if (
                not isinstance(usage, Usage)
                or not isinstance(actual_cost, Decimal)
                or actual_cost != tariff.cost(usage.input_tokens, usage.output_tokens)
            ):
                if isinstance(usage, Usage):
                    await cost_repo.mark_uncertain(reservation_id, consume_cap=True)
                    raise UnsafeUsageError("Ответ модели не прошёл проверку стоимости")
                raise ClassificationError("Модель не предоставила проверяемые usage и стоимость")
            if usage.input_tokens > reserved_input_tokens or usage.output_tokens > max_tokens:
                await cost_repo.mark_uncertain(reservation_id, consume_cap=True)
                raise UnsafeUsageError(
                    "usage превышает зарезервированные пределы; бюджет заблокирован"
                )
        except Exception as exc:
            # A transport failure may occur after the provider billed the request.
            # Keep the full reservation and claim the reviews to prevent retries.
            failed_usage = getattr(exc, "usage", None)
            failed_cost = getattr(exc, "cost_usd", None)
            valid_failed_usage = (
                isinstance(failed_usage, Usage)
                and isinstance(failed_cost, Decimal)
                and failed_cost
                == tariff.cost(failed_usage.input_tokens, failed_usage.output_tokens)
                and failed_usage.input_tokens <= reserved_input_tokens
                and failed_usage.output_tokens <= max_tokens
            )
            if valid_failed_usage:
                assert isinstance(failed_usage, Usage)
                assert isinstance(failed_cost, Decimal)
                await cost_repo.settle(
                    reservation_id,
                    input_tokens=failed_usage.input_tokens,
                    output_tokens=failed_usage.output_tokens,
                    actual_cost_usd=failed_cost,
                    settled_at=clock.now(),
                )
                await session.commit()
            else:
                await cost_repo.mark_uncertain(
                    reservation_id, consume_cap=isinstance(failed_usage, Usage)
                )
                if isinstance(failed_usage, Usage):
                    raise
            if _is_provider_failure(exc):
                failed += len(reviews)
                consecutive_provider_failures += 1
                logger.warning(
                    "Пакет AI не обработан ({}); отзывы закреплены за запросом: {}",
                    type(exc).__name__,
                    exc,
                )
                if consecutive_provider_failures >= 3:
                    deferred = await _count_pending(
                        repo,
                        until=until,
                        max_stars=max_stars,
                        feedback_ids=(
                            retry_feedback_ids if retry_feedback_ids is not None else feedback_ids
                        ),
                        retry=retry_feedback_ids is not None,
                    )
                    return ClassifyResult(
                        classified=classified,
                        batches=batches,
                        attempted=attempted,
                        failed=failed,
                        deferred=deferred,
                        stopped_due_provider=True,
                    )
                continue
            raise
        await cost_repo.settle(
            reservation_id,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            actual_cost_usd=actual_cost,
            settled_at=clock.now(),
        )
        await session.commit()
        expected_ids = {row.feedback_id for row in reviews}
        try:
            returned_ids = set(answer)
        except (TypeError, ValueError):
            returned_ids = set()
        if returned_ids != expected_ids:
            failed += len(reviews)
            consecutive_provider_failures += 1
            logger.warning("Пакет AI вернул неполный набор ID; отзывы требуют ручного разбора")
            if consecutive_provider_failures >= 3:
                return ClassifyResult(
                    classified=classified,
                    batches=batches,
                    attempted=attempted,
                    failed=failed,
                    deferred=await _count_pending(
                        repo,
                        until=until,
                        max_stars=max_stars,
                        feedback_ids=(
                            retry_feedback_ids if retry_feedback_ids is not None else feedback_ids
                        ),
                        retry=retry_feedback_ids is not None,
                    ),
                    stopped_due_provider=True,
                )
            continue

        assignments: list[tuple[str, int]] = []
        try:
            for review in reviews:
                selected = set(answer[review.feedback_id]) or {fallback}
                if not selected <= reasons.keys():
                    raise ClassificationError(
                        f"Модель вернула неизвестную причину для {review.feedback_id}"
                    )
                assignments.extend((review.feedback_id, reason_id) for reason_id in selected)
        except (KeyError, TypeError, ClassificationError) as exc:
            failed += len(reviews)
            consecutive_provider_failures += 1
            logger.warning("Пакет AI не прошёл проверку меток; нужен ручной разбор: {}", exc)
            if consecutive_provider_failures >= 3:
                return ClassifyResult(
                    classified=classified,
                    batches=batches,
                    attempted=attempted,
                    failed=failed,
                    deferred=await _count_pending(
                        repo,
                        until=until,
                        max_stars=max_stars,
                        feedback_ids=(
                            retry_feedback_ids if retry_feedback_ids is not None else feedback_ids
                        ),
                        retry=retry_feedback_ids is not None,
                    ),
                    stopped_due_provider=True,
                )
            continue
        await repo.save(assignments, model=classifier.model, classified_at=clock.now())
        await session.commit()
        classified += len(reviews)
        consecutive_provider_failures = 0
        logger.info("Классифицировано отзывов: {}", classified)
    deferred = (
        await _count_pending(
            repo,
            until=until,
            max_stars=max_stars,
            feedback_ids=retry_feedback_ids if retry_feedback_ids is not None else feedback_ids,
            retry=retry_feedback_ids is not None,
        )
        if limit is not None
        else 0
    )
    return ClassifyResult(
        classified=classified,
        batches=batches,
        attempted=attempted,
        failed=failed,
        deferred=deferred,
    )


def _is_provider_failure(exc: Exception) -> bool:
    if isinstance(exc, UnsafeUsageError):
        return False
    if isinstance(exc, ClassificationError):
        return True
    if isinstance(exc, httpx.RequestError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and (
        exc.response.status_code == 429 or exc.response.status_code >= 500
    )


async def _count_pending(
    repo: ReasonRepository,
    *,
    until: dt.date,
    max_stars: int,
    feedback_ids: list[str] | None,
    retry: bool,
) -> int:
    if retry:
        return await repo.count_pending_retry(
            until=until, max_stars=max_stars, feedback_ids=feedback_ids or []
        )
    return await repo.count_pending(until=until, max_stars=max_stars, feedback_ids=feedback_ids)
