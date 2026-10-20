"""Token estimates and tariff arithmetic used by paid classification admission."""

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import pytest
from app.config import AiConfig, Secrets, load_settings
from app.jobs import _verified_tariff
from app.repositories.ai_cost import (
    AiCostRepository,
    CostLimitExceededError,
    RetryNotAllowedError,
)
from app.repositories.reasons import ReasonRepository
from app.services.ai_cost import ModelTariff, completion_token_limit, estimate_input_tokens


class _Result:
    def __init__(self, value: object = None, rows: list[object] | None = None) -> None:
        self.value = value
        self.rows = rows or []

    def scalar_one(self) -> object:
        return self.value

    def first(self) -> object:
        return self.rows[0] if self.rows else None

    def all(self) -> list[object]:
        return self.rows


class _Session:
    def __init__(
        self,
        *,
        uncertain: bool = False,
        cap_sentinel: bool = False,
        spent: Decimal = Decimal("0"),
        retry_rows: list[object] | None = None,
    ) -> None:
        self.uncertain = uncertain
        self.cap_sentinel = cap_sentinel
        self.spent = spent
        self.retry_rows = retry_rows or []
        self.statements: list[str] = []
        self.params: list[dict[str, object] | None] = []
        self.rollbacks = 0
        self.commits = 0

    async def execute(self, statement: object, params: dict[str, object] | None = None) -> _Result:
        query = str(statement)
        self.statements.append(query)
        self.params.append(params)
        if "consumes_monthly_cap" in query:
            return _Result(self.cap_sentinel)
        if "join wb.ai_review_claims c" in query and "not exists" in query:
            rows = self.retry_rows
            if "original_request.status in ('settled', 'uncertain')" in query:
                rows = [row for row in rows if row.status in {"settled", "uncertain"}]
            return _Result(rows=rows)
        if "coalesce(sum" in query:
            return _Result(self.spent)
        if "returning id" in query:
            return _Result(1)
        return _Result()

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def commit(self) -> None:
        self.commits += 1


def test_tariff_calculates_separate_input_and_output_costs() -> None:
    tariff = ModelTariff("model-x", Decimal("0.50"), Decimal("1.25"))

    assert tariff.cost(2_000_000, 400_000) == Decimal("1.5")


def test_unknown_or_invalid_tariff_cannot_be_created() -> None:
    with pytest.raises(ValueError):
        ModelTariff("", 1, 1)
    with pytest.raises(ValueError):
        ModelTariff("model-x", -1, 1)


def test_automatic_ai_defaults_to_off() -> None:
    assert (
        AiConfig.model_validate(
            load_settings().ai.model_dump(exclude={"automatic_enabled"})
        ).automatic_enabled
        is False
    )


def test_project_config_uses_verified_tariff() -> None:
    settings = load_settings()
    secrets = Secrets(
        _env_file=None,
        database_url="postgresql+asyncpg://ignored",
        llm_api_key="DemoRedacted",
        llm_base_url="https://internal.example.invalid/endpoint",
        llm_model_primary="deepseek-v4.1-flash",
    )

    tariff = _verified_tariff(settings, secrets)
    assert tariff.cost(1_000_000, 1_000_000) == Decimal("2")


def test_reservation_uses_utf8_payload_bound_and_bounded_completion() -> None:
    estimate = estimate_input_tokens({1: "протекают"}, [("id-1", "текст", 2)])

    assert estimate >= len("протекаюттекст".encode())
    assert completion_token_limit(1) == 128
    assert completion_token_limit(100) == 4096


def test_reservation_bound_includes_current_product_context_bytes() -> None:
    without_product = estimate_input_tokens({1: "протекают"}, [("id-1", "текст", 2)])
    with_product = estimate_input_tokens(
        {1: "протекают"},
        [("id-1", "текст", 2)],
        product_context={"id-1": ("Подгузники размер 6–10 кг", "6–10 кг")},
    )

    assert with_product >= without_product + len("Подгузники размер 6–10 кг".encode())


async def test_bounded_uncertain_usage_allows_next_request_and_keeps_charge_in_sum() -> None:
    session = _Session(uncertain=True)

    await AiCostRepository(session).reserve(
        model="model-x",
        feedback_ids=["review-2"],
        estimated_cost_usd=Decimal("0.01"),
        reserved_at=dt.datetime(2026, 10, 14, tzinfo=dt.UTC),
    )

    assert session.rollbacks == 0
    assert any("insert into wb.ai_cost_requests" in query for query in session.statements)
    assert any("coalesce(sum" in query for query in session.statements)


async def test_over_estimate_sentinel_blocks_every_further_request() -> None:
    session = _Session(uncertain=True, cap_sentinel=True, spent=Decimal("100.00"))

    with pytest.raises(CostLimitExceededError, match="uncertain"):
        await AiCostRepository(session).reserve(
            model="model-x",
            feedback_ids=["review-2"],
            estimated_cost_usd=Decimal("0.01"),
            reserved_at=dt.datetime(2026, 10, 14, tzinfo=dt.UTC),
        )

    assert session.rollbacks == 1
    assert not any("insert into wb.ai_cost_requests" in query for query in session.statements)


async def test_explicit_manual_admission_keeps_uncertain_charge_and_records_override() -> None:
    session = _Session(uncertain=True, spent=Decimal("0.005368"))

    request_id = await AiCostRepository(session).reserve(
        model="model-x",
        feedback_ids=["fresh-review"],
        estimated_cost_usd=Decimal("0.01"),
        reserved_at=dt.datetime(2026, 10, 15, tzinfo=dt.UTC),
        allow_uncertain_manual=True,
    )

    assert request_id == 1
    assert session.commits == 1
    assert any("insert into wb.ai_uncertain_admissions" in query for query in session.statements)
    assert not any("update wb.ai_cost_requests" in query for query in session.statements)


async def test_manual_admission_still_rejects_total_over_monthly_cap() -> None:
    session = _Session(uncertain=True, spent=Decimal("99.995"))

    with pytest.raises(CostLimitExceededError):
        await AiCostRepository(session).reserve(
            model="model-x",
            feedback_ids=["fresh-review"],
            estimated_cost_usd=Decimal("0.01"),
            reserved_at=dt.datetime(2026, 10, 15, tzinfo=dt.UTC),
            allow_uncertain_manual=True,
        )

    assert session.rollbacks == 1
    assert not any("insert into wb.ai_cost_requests" in query for query in session.statements)


async def test_pending_reviews_excludes_claimed_feedback() -> None:
    session = _Session()

    await ReasonRepository(session).pending(until=dt.date(2026, 10, 1), max_stars=3, limit=10)

    assert "wb.ai_review_claims" in session.statements[0]


async def test_budget_month_uses_moscow_calendar() -> None:
    session = _Session()
    await AiCostRepository(session).reserve(
        model="model-x",
        feedback_ids=["review-3"],
        estimated_cost_usd=Decimal("0.01"),
        reserved_at=dt.datetime(2026, 9, 30, 22, 30, tzinfo=dt.UTC),
    )

    monthly_query_index = next(
        i for i, query in enumerate(session.statements) if "coalesce(sum" in query
    )
    assert session.params[monthly_query_index] == {"month_start": dt.date(2026, 10, 1)}


async def test_reserve_retry_keeps_original_claim_and_links_new_request() -> None:
    session = _Session(
        retry_rows=[SimpleNamespace(feedback_id="review-old", request_id=41, status="uncertain")]
    )

    request_id = await AiCostRepository(session).reserve_retry(
        model="model-x",
        feedback_ids=["review-old"],
        estimated_cost_usd=Decimal("0.01"),
        reserved_at=dt.datetime(2026, 10, 14, tzinfo=dt.UTC),
    )

    assert request_id == 1
    retry_insert = next(
        (query for query in session.statements if "insert into wb.ai_review_retries" in query),
        None,
    )
    assert retry_insert is not None
    assert session.commits == 1


async def test_reserve_retry_rejects_any_id_without_an_eligible_original_claim() -> None:
    session = _Session()

    with pytest.raises(RetryNotAllowedError, match="eligible claimed reviews"):
        await AiCostRepository(session).reserve_retry(
            model="model-x",
            feedback_ids=["never-claimed"],
            estimated_cost_usd=Decimal("0.01"),
            reserved_at=dt.datetime(2026, 10, 14, tzinfo=dt.UTC),
        )

    assert session.rollbacks == 1
    assert not any("insert into wb.ai_cost_requests" in query for query in session.statements)


async def test_reserve_retry_rejects_review_whose_original_request_is_still_running() -> None:
    session = _Session(
        retry_rows=[
            SimpleNamespace(feedback_id="in-flight-review", request_id=41, status="reserved")
        ]
    )

    with pytest.raises(RetryNotAllowedError, match="eligible claimed reviews"):
        await AiCostRepository(session).reserve_retry(
            model="model-x",
            feedback_ids=["in-flight-review"],
            estimated_cost_usd=Decimal("0.01"),
            reserved_at=dt.datetime(2026, 10, 14, tzinfo=dt.UTC),
        )

    assert session.rollbacks == 1
    assert not any("insert into wb.ai_cost_requests" in query for query in session.statements)
