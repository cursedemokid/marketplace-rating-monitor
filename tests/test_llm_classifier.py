"""Контракт HTTP-адаптера классификации без внешней сети."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from app.services.classify import ReviewInput


@respx.mock
async def test_classifier_sends_only_approved_reasons_and_parses_response() -> None:
    from app.adapters.llm.classifier import ChatCompletionClassifier, ModelTariff

    route = respx.post("https://llm.example/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"results": [{"feedback_id": "review-1", "reason_ids": [4]}]}
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            },
        )
    )

    async with ChatCompletionClassifier(
        base_url="https://llm.example/v1",
        api_key="DemoRedacted",
        model="test-model",
        tariff=ModelTariff("test-model", 1.0, 2.0),
    ) as classifier:
        result = await classifier.classify(
            [
                ReviewInput(
                    "review-1",
                    "Протекают",
                    2,
                    product_title="Подгузники https://catalog.example/item",
                    product_size="6–10 кг",
                )
            ],
            {4: "протекают"},
        )

    assert result == {"review-1": [4]}
    assert result.usage.input_tokens == 12
    assert result.usage.output_tokens == 3
    assert str(result.cost_usd) == "0.000018"
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer DemoRedacted"
    payload = json.loads(request.content)
    assert payload["model"] == "test-model"
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["response_format"] == {"type": "json_object"}
    assert "протекают" in str(payload)
    assert "review-1" in str(payload)
    assert "Протекают" in str(payload)
    user_payload = json.loads(payload["messages"][1]["content"])
    assert user_payload["reviews"][0]["stars"] == 2
    assert user_payload["reviews"][0]["product"] == {
        "title": "Подгузники [ссылка]",
        "size": "6–10 кг",
        "source": "current_catalog",
        "temporal_note": "current listing; may differ from listing at review time",
    }
    policy = payload["messages"][0]["content"]
    assert "несколько причин" in policy
    assert "слабый или иной аромат" in policy
    assert "нечитаемый отзыв" in policy


@respx.mock
async def test_classifier_uses_configured_response_timeout() -> None:
    from app.adapters.llm.classifier import ChatCompletionClassifier, ModelTariff

    route = respx.post("https://llm.example/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"results":[]}'}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )
    )
    async with ChatCompletionClassifier(
        base_url="https://llm.example/v1",
        api_key="DemoRedacted",
        model="test-model",
        tariff=ModelTariff("test-model", 1, 2),
    ) as classifier:
        await classifier.classify([ReviewInput("review-1", "Текст", 1)], {4: "протекают"})

    assert route.calls[0].request.extensions["timeout"]["read"] == 180.0


def test_classifier_rejects_nonpositive_timeout() -> None:
    from app.adapters.llm.classifier import ChatCompletionClassifier, ModelTariff

    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        ChatCompletionClassifier(
            base_url="https://llm.example/v1",
            api_key="DemoRedacted",
            model="test-model",
            tariff=ModelTariff("test-model", 1, 2),
            timeout_seconds=0,
        )


@respx.mock
async def test_classifier_fails_closed_when_usage_is_missing() -> None:
    from app.adapters.llm.classifier import ChatCompletionClassifier, ModelTariff
    from app.services.classify import ClassificationError

    respx.post("https://llm.example/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"results": []}'}}]},
        )
    )
    async with ChatCompletionClassifier(
        base_url="https://llm.example/v1",
        api_key="DemoRedacted",
        model="test-model",
        tariff=ModelTariff("test-model", 1.0, 2.0),
    ) as classifier:
        with pytest.raises(ClassificationError, match="usage"):
            await classifier.classify([ReviewInput("review-1", "Текст", 1)], {4: "протекают"})


@respx.mock
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
async def test_classifier_accepts_provider_json_inside_single_markdown_fence(
    newline: str,
) -> None:
    from app.adapters.llm.classifier import ChatCompletionClassifier, ModelTariff

    respx.post("https://llm.example/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": newline.join(
                                [
                                    "```json",
                                    '{"results":[{"feedback_id":"review-1","reason_ids":[4]}]}',
                                    "```",
                                ]
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 18},
            },
        )
    )
    async with ChatCompletionClassifier(
        base_url="https://llm.example/v1",
        api_key="DemoRedacted",
        model="test-model",
        tariff=ModelTariff("test-model", 1, 2),
    ) as classifier:
        result = await classifier.classify(
            [ReviewInput("review-1", "Протекают", 1)], {4: "протекают"}
        )

    assert result == {"review-1": [4]}
    assert result.usage.output_tokens == 18


@respx.mock
async def test_name_proposal_uses_json_and_accounts_for_usage() -> None:
    from app.adapters.llm.classifier import ChatCompletionClassifier, NameProposalResponse
    from app.services.ai_cost import ModelTariff

    route = respx.post("https://llm.example/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"name":"Протекают"}'}}],
                "usage": {"prompt_tokens": 14, "completion_tokens": 3},
            },
        )
    )
    async with ChatCompletionClassifier(
        base_url="https://llm.example/v1",
        api_key="DemoRedacted",
        model="test-model",
        tariff=ModelTariff("test-model", 1, 2),
    ) as classifier:
        result = await classifier.propose_name(["Протекает"], 32)

    assert isinstance(result, NameProposalResponse)
    assert result.name == "Протекают"
    assert result.usage.input_tokens == 14 and result.usage.output_tokens == 3
    assert result.cost_usd == ModelTariff("test-model", 1, 2).cost(14, 3)
    body = json.loads(route.calls[0].request.content)
    assert body["max_tokens"] == 32
    assert body["thinking"] == {"type": "disabled"}
    assert body["response_format"] == {"type": "json_object"}
    assert "Протекает" in body["messages"][1]["content"]
