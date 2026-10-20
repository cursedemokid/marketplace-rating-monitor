"""Классификатор причин через совместимый с Chat Completions HTTP API."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from types import TracebackType
from typing import Any, Self

import httpx
from pydantic import BaseModel, ValidationError

from app.services.ai_cost import (
    CLASSIFIER_SYSTEM_PROMPT,
    JSON_RESPONSE_FORMAT,
    NAME_PROPOSAL_SYSTEM_PROMPT,
    THINKING_MODE,
    ModelTariff,
    Usage,
    build_classifier_user_content,
    completion_token_limit,
)
from app.services.classify import ClassificationError, ReviewInput


class ClassificationResponse(dict[str, list[int]]):
    """Mapping of labels with verified provider usage and calculated cost."""

    def __init__(self, labels: dict[str, list[int]], *, usage: Usage, cost_usd: Decimal) -> None:
        super().__init__(labels)
        self.usage = usage
        self.cost_usd = cost_usd


@dataclass(frozen=True, slots=True)
class NameProposalResponse:
    name: str
    usage: Usage
    cost_usd: Decimal


class _Item(BaseModel):
    feedback_id: str
    reason_ids: list[int]


class _Answer(BaseModel):
    results: list[_Item]


class _NameAnswer(BaseModel):
    name: str


class ChatCompletionClassifier:
    """Классифицирует очищенные отзывы с текущим названием и размером товара."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        tariff: ModelTariff,
        max_output_tokens: int = 4096,
        timeout_seconds: float = 180.0,
    ) -> None:
        if tariff.model != model:
            raise ValueError("tariff model does not match configured model")
        if max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.model = model
        self.tariff = tariff
        self.max_output_tokens = max_output_tokens
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout_seconds
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._client.aclose()

    async def classify(
        self, reviews: Sequence[ReviewInput], reasons: dict[int, str]
    ) -> ClassificationResponse:
        max_tokens = min(completion_token_limit(len(reviews)), self.max_output_tokens)
        request_body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "thinking": THINKING_MODE,
            "response_format": JSON_RESPONSE_FORMAT,
            "messages": [
                {
                    "role": "system",
                    "content": CLASSIFIER_SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": build_classifier_user_content(
                        reasons,
                        [(review.feedback_id, review.text, review.stars) for review in reviews],
                        product_context={
                            review.feedback_id: (review.product_title, review.product_size)
                            for review in reviews
                        },
                    ),
                },
            ],
        }
        response = await self._client.post(self._url, json=request_body)
        response.raise_for_status()
        try:
            payload: dict[str, Any] = response.json()
            raw_usage = payload["usage"]
            prompt_tokens = raw_usage["prompt_tokens"]
            completion_tokens = raw_usage["completion_tokens"]
            if (
                isinstance(prompt_tokens, bool)
                or not isinstance(prompt_tokens, int)
                or prompt_tokens < 0
                or isinstance(completion_tokens, bool)
                or not isinstance(completion_tokens, int)
                or completion_tokens < 0
            ):
                raise TypeError("usage tokens are invalid")
        except (KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
            raise ClassificationError("Модель вернула некорректный ответ или usage") from exc
        usage = Usage(prompt_tokens, completion_tokens)
        cost_usd = self.tariff.cost(prompt_tokens, completion_tokens)
        try:
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("content не строка")
            fenced = re.fullmatch(
                r"\s*```(?:json)?[ \t]*\r?\n(?P<body>.*?)\r?\n```\s*",
                content,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if fenced is not None:
                content = fenced.group("body")
            answer = _Answer.model_validate_json(content)
        except (KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
            error = ClassificationError("Модель вернула ответ не по JSON-контракту")
            error.usage = usage  # type: ignore[attr-defined]
            error.cost_usd = cost_usd  # type: ignore[attr-defined]
            raise error from exc
        result = {item.feedback_id: item.reason_ids for item in answer.results}
        if len(result) != len(answer.results):
            error = ClassificationError("Модель повторила feedback_id в ответе")
            error.usage = usage  # type: ignore[attr-defined]
            error.cost_usd = cost_usd  # type: ignore[attr-defined]
            raise error
        return ClassificationResponse(
            result,
            usage=usage,
            cost_usd=cost_usd,
        )

    async def propose_name(
        self, samples: Sequence[str], max_output_tokens: int
    ) -> NameProposalResponse:
        if max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        token_limit = min(max_output_tokens, self.max_output_tokens)
        request_body = {
            "model": self.model,
            "max_tokens": token_limit,
            "thinking": THINKING_MODE,
            "response_format": JSON_RESPONSE_FORMAT,
            "messages": [
                {"role": "system", "content": NAME_PROPOSAL_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(list(samples), ensure_ascii=False, separators=(",", ":")),
                },
            ],
        }
        response = await self._client.post(self._url, json=request_body)
        response.raise_for_status()
        try:
            payload: dict[str, Any] = response.json()
            raw_usage = payload["usage"]
            prompt_tokens = raw_usage["prompt_tokens"]
            completion_tokens = raw_usage["completion_tokens"]
            if (
                isinstance(prompt_tokens, bool)
                or not isinstance(prompt_tokens, int)
                or prompt_tokens < 0
                or isinstance(completion_tokens, bool)
                or not isinstance(completion_tokens, int)
                or completion_tokens < 0
            ):
                raise TypeError("usage tokens are invalid")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ClassificationError("Модель вернула некорректный usage для названия") from exc
        usage = Usage(prompt_tokens, completion_tokens)
        cost_usd = self.tariff.cost(prompt_tokens, completion_tokens)
        try:
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("content is not text")
            name = _NameAnswer.model_validate_json(content).name.strip()
            if not name or len(name) > 80:
                raise ValueError("name must contain 1 to 80 characters")
        except (KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
            error = ClassificationError("Модель не вернула допустимое название причины")
            error.usage = usage  # type: ignore[attr-defined]
            error.cost_usd = cost_usd  # type: ignore[attr-defined]
            raise error from exc
        return NameProposalResponse(name=name, usage=usage, cost_usd=cost_usd)
