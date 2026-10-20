"""Budgeted, one-shot AI naming of discovery candidates."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import pytest
from app.adapters.llm.classifier import NameProposalResponse
from app.repositories.ai_cost import AiCostRepository
from app.repositories.ai_naming import AiNamingRepository
from app.services.ai_cost import ModelTariff, Usage


class _Client:
    model = "model-name"

    def __init__(self) -> None:
        self.calls = 0
        self.samples: list[str] = []

    async def propose_name(self, samples: list[str], max_output_tokens: int) -> object:
        self.calls += 1
        self.samples = samples
        return NameProposalResponse("Протекают", Usage(12, 4), Decimal("0.00002"))


class _Clock:
    def now(self):
        return dt.datetime(2026, 10, 14, tzinfo=dt.UTC)


class _Session:
    async def commit(self) -> None:
        pass


class _DbRow:
    status = "pending"
    proposed_name = None


class _ReserveSession:
    def __init__(self) -> None:
        self.claimed = False
        self.inserts = 0
        self.rollbacks = 0

    async def execute(self, statement, params=None):
        query = str(statement)
        if "consumes_monthly_cap" in query:
            return SimpleNamespace(scalar_one=lambda: False)
        if "coalesce(sum" in query:
            return SimpleNamespace(scalar_one=lambda: Decimal("0"))
        if "from wb.discovery_candidates" in query:
            return SimpleNamespace(first=lambda: _DbRow())
        if "from wb.ai_naming_requests" in query:
            return SimpleNamespace(scalar_one=lambda: self.claimed)
        if "insert into wb.ai_cost_requests" in query:
            self.inserts += 1
            return SimpleNamespace(scalar_one=lambda: 500 + self.inserts)
        if "insert into wb.ai_naming_requests" in query:
            self.claimed = True
            return SimpleNamespace()
        return SimpleNamespace()

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        self.rollbacks += 1


class _UpdateSession:
    statement = ""

    async def execute(self, statement, params=None):
        self.statement = str(statement)
        return SimpleNamespace(scalar_one_or_none=lambda: 7)


async def test_propose_name_sanitizes_samples_records_usage_and_only_sets_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.ai_naming as naming

    saved: list[tuple[int, str]] = []
    settled: list[dict[str, object]] = []
    monkeypatch.setattr(
        naming.AiNamingRepository,
        "sample_texts",
        lambda *a, **k: _async(["протекает DemoPhone DemoEmail"]),
    )

    async def save_proposal(_repo, candidate_id: int, name: str) -> bool:
        saved.append((candidate_id, name))
        return True

    async def record_settlement(_repo, request_id: int, **values: object) -> None:
        settled.append({"request_id": request_id, **values})

    monkeypatch.setattr(naming.AiNamingRepository, "save_proposed_name", save_proposal)
    monkeypatch.setattr(
        naming.AiCostRepository, "reserve_candidate_name", lambda *a, **k: _async(101)
    )
    monkeypatch.setattr(naming.AiCostRepository, "settle", record_settlement)

    client = _Client()
    result = await naming.propose_name(
        _Session(),
        7,
        client,
        ModelTariff("model-name", 1, 2),
        _Clock(),
        max_input_tokens=2000,
        max_output_tokens=64,
    )

    assert result == "Протекают"
    assert client.calls == 1
    assert "+7" not in client.samples[0] and "@example.org" not in client.samples[0]
    assert saved == [(7, "Протекают")]
    assert settled[0]["request_id"] == 101
    assert settled[0]["input_tokens"] == 12
    assert settled[0]["output_tokens"] == 4
    assert settled[0]["actual_cost_usd"] == Decimal("0.00002")


async def test_uncertain_or_existing_candidate_never_calls_name_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.ai_naming as naming

    monkeypatch.setattr(
        naming.AiNamingRepository, "sample_texts", lambda *a, **k: _async(["текст"])
    )
    monkeypatch.setattr(
        naming.AiCostRepository, "reserve_candidate_name", lambda *a, **k: _async(None)
    )
    client = _Client()

    result = await naming.propose_name(
        _Session(),
        7,
        client,
        ModelTariff("model-name", 1, 2),
        _Clock(),
        max_input_tokens=2000,
        max_output_tokens=64,
    )

    assert result is None
    assert client.calls == 0


async def test_unknown_tariff_fails_before_reading_or_sending_samples() -> None:
    import app.services.ai_naming as naming
    from app.services.classify import ClassificationError

    client = _Client()
    with pytest.raises(ClassificationError, match="тариф"):
        await naming.propose_name(
            _Session(),
            7,
            client,
            None,
            _Clock(),
            max_input_tokens=2000,
            max_output_tokens=64,
        )

    assert client.calls == 0


async def test_tariff_check_blocks_naming_before_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.ai_naming as naming

    monkeypatch.setattr(
        naming.AiNamingRepository, "sample_texts", lambda *a, **k: _async(["текст"])
    )
    reserved = False

    async def reserve(*_args: object, **_kwargs: object) -> int:
        nonlocal reserved
        reserved = True
        return 102

    async def reject_tariff() -> None:
        raise RuntimeError("tariff changed")

    monkeypatch.setattr(naming.AiCostRepository, "reserve_candidate_name", reserve)
    client = _Client()
    with pytest.raises(RuntimeError, match="tariff changed"):
        await naming.propose_name(
            _Session(),
            7,
            client,
            ModelTariff("model-name", 1, 2),
            _Clock(),
            max_input_tokens=2000,
            max_output_tokens=64,
            before_paid_request=reject_tariff,
        )

    assert not reserved
    assert client.calls == 0


async def test_missing_usage_marks_the_one_shot_call_uncertain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.ai_naming as naming
    from app.services.classify import ClassificationError

    uncertain: list[int] = []
    monkeypatch.setattr(
        naming.AiNamingRepository, "sample_texts", lambda *a, **k: _async(["текст"])
    )
    monkeypatch.setattr(
        naming.AiCostRepository, "reserve_candidate_name", lambda *a, **k: _async(102)
    )
    monkeypatch.setattr(
        naming.AiCostRepository, "mark_uncertain", lambda *a, **k: _async(uncertain.append(a[1]))
    )

    class MissingUsage(_Client):
        async def propose_name(self, samples: list[str], max_output_tokens: int) -> object:
            self.calls += 1
            return SimpleNamespace(name="Протекают")

    with pytest.raises(ClassificationError, match="usage"):
        await naming.propose_name(
            _Session(),
            7,
            MissingUsage(),
            ModelTariff("model-name", 1, 2),
            _Clock(),
            max_input_tokens=2000,
            max_output_tokens=64,
        )

    assert uncertain == [102]


async def test_repository_reserves_a_candidate_name_only_once() -> None:
    session = _ReserveSession()
    repo = AiCostRepository(session)
    args = {
        "candidate_id": 7,
        "model": "model-name",
        "estimated_cost_usd": Decimal("0.01"),
        "reserved_at": dt.datetime(2026, 10, 14, tzinfo=dt.UTC),
    }

    first = await repo.reserve_candidate_name(**args)
    second = await repo.reserve_candidate_name(**args)

    assert first == 501
    assert second is None
    assert session.inserts == 1
    assert session.rollbacks == 1


async def test_saving_ai_name_only_sets_proposed_name() -> None:
    session = _UpdateSession()

    saved = await AiNamingRepository(session).save_proposed_name(7, "Протекают")

    assignment = session.statement.split("where", maxsplit=1)[0]
    assert saved
    assert "set proposed_name =" in assignment
    assert "status =" not in assignment


def _async(value):
    async def result():
        return value

    return result()
