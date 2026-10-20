from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from uuid import uuid4

import numpy as np
import pytest
from app.services.discover import DiscoveryReview, cluster_reviews
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class FakeEmbedder:
    def encode(self, texts: list[str], **_kwargs: object) -> np.ndarray:
        return np.asarray([[float(index), 1.0] for index, _ in enumerate(texts)])


class FakeClusterer:
    def fit_predict(self, _embeddings: np.ndarray) -> np.ndarray:
        return np.asarray([0, 0, 1, -1])


def test_cluster_reviews_returns_only_repeated_non_noise_groups() -> None:
    reviews = [DiscoveryReview(f"review-{i}", f"text {i}") for i in range(4)]

    groups = cluster_reviews(
        reviews,
        embedder=FakeEmbedder(),
        clusterer=FakeClusterer(),
        min_cluster_size=2,
    )

    assert groups == [["review-0", "review-1"]]


async def test_discovery_run_does_not_call_name_proposer_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import discover

    stored: list[tuple[list[list[str]], list[str | None] | None]] = []
    examined: list[list[str]] = []

    class FakeRepository:
        def __init__(self, _session: object) -> None:
            pass

        async def repeated_unassigned(self, **_kwargs: object) -> list[DiscoveryReview]:
            return [DiscoveryReview(f"review-{index}", f"body {index}") for index in range(4)]

        async def create_candidates(
            self, groups: list[list[str]], *, names: list[str | None] | None = None
        ) -> list[int]:
            stored.append((groups, names))
            return [42]

        async def mark_examined(self, feedback_ids: list[str]) -> None:
            examined.append(feedback_ids)

    monkeypatch.setattr(discover, "DiscoveryRepository", FakeRepository)
    result = await discover.run(
        object(),
        until=dt.date(2026, 10, 1),
        min_cluster_size=2,
        embedder=FakeEmbedder(),
        clusterer=FakeClusterer(),
    )

    assert result.created_candidates == (42,)
    assert stored == [([["review-0", "review-1"]], [None])]
    assert examined == [[f"review-{index}" for index in range(4)]]


async def test_name_proposer_is_an_opt_in_hook_receiving_only_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import discover

    stored: list[list[str | None] | None] = []

    class FakeRepository:
        def __init__(self, _session: object) -> None:
            pass

        async def repeated_unassigned(self, **_kwargs: object) -> list[DiscoveryReview]:
            return [DiscoveryReview(f"review-{index}", f"body {index}") for index in range(4)]

        async def create_candidates(
            self, _groups: list[list[str]], *, names: list[str | None] | None = None
        ) -> list[int]:
            stored.append(names)
            return [43]

        async def mark_examined(self, _feedback_ids: list[str]) -> None:
            pass

    seen: list[list[str]] = []

    def propose(texts: list[str]) -> str:
        seen.append(texts)
        return "Suggested cause"

    monkeypatch.setattr(discover, "DiscoveryRepository", FakeRepository)
    result = await discover.run(
        object(),
        until=dt.date(2026, 10, 1),
        min_cluster_size=2,
        embedder=FakeEmbedder(),
        clusterer=FakeClusterer(),
        name_proposer=propose,
    )

    assert result.created_candidates == (43,)
    assert seen == [["body 0", "body 1"]]
    assert stored == [["Suggested cause"]]


async def test_candidates_keep_source_ids_and_admin_actions_are_explicit(
    session: AsyncSession,
) -> None:
    from app.repositories.discovery import DiscoveryRepository

    suffix = uuid4().hex
    nm_id = 900_000_000 + uuid4().int % 99_000_000
    await session.execute(
        text("insert into wb.products (nm_id, vendor_code, updated_at) values (:nm, :code, now())"),
        {"nm": nm_id, "code": f"DISC-{suffix}"},
    )
    reason_ids = (
        await session.execute(
            text("""
                select name, id from wb.negative_reasons
                 where name in ('причина не указана', 'протекают')
            """)
        )
    ).all()
    reason_id_by_name = {str(row.name): int(row.id) for row in reason_ids}
    fallback_id = reason_id_by_name["причина не указана"]
    known_id = reason_id_by_name["протекают"]
    source_ids = [f"discover-{suffix}-{index}" for index in range(8)]
    for index, feedback_id in enumerate(source_ids):
        await session.execute(
            text("""
                insert into wb.feedbacks
                    (id, nm_id, valuation, text, created_at, created_date, imported_at)
                values (:id, :nm, :stars, :body, now(), :created_date, now())
            """),
            {
                "id": feedback_id,
                "nm": nm_id,
                "stars": 5 if index == 7 else 2,
                "body": f"Review body {index}",
                "created_date": dt.date(2026, 10, 1),
            },
        )
        await session.execute(
            text("""
                insert into wb.feedback_reasons (feedback_id, reason_id, model, classified_at)
                values (:id, :reason, 'test', now())
            """),
            {"id": feedback_id, "reason": fallback_id},
        )
    await session.execute(
        text("""
            insert into wb.feedback_reasons (feedback_id, reason_id, model, classified_at)
            values (:id, :reason, 'test', now())
        """),
        {"id": source_ids[6], "reason": known_id},
    )

    repo = DiscoveryRepository(session)
    pending = await repo.repeated_unassigned(until=dt.date(2026, 10, 2), limit=100)
    # A review can have a known reason and an additional new complaint.
    expected = set(source_ids[:7])
    assert {review.feedback_id for review in pending} == expected
    first, second, rejected = await repo.create_candidates(
        [source_ids[:2], source_ids[2:4], source_ids[4:6]],
        names=[None, "Proposed", "Rejected"],
    )
    candidates = await repo.candidates(status="pending")
    candidate = next(candidate for candidate in candidates if candidate.id == first)
    assert candidate.name is None
    assert candidate.feedback_ids == tuple(source_ids[:2])
    assert (
        await session.execute(
            text("select count(*) from wb.negative_reasons where name = 'Proposed'")
        )
    ).scalar_one() == 0

    await repo.merge(second, first)
    await repo.rename(first, "  Test cause  ")
    candidate = next(item for item in await repo.candidates(status="pending") if item.id == first)
    assert candidate.name == "Test cause"
    await repo.reject(rejected)
    reason_id = await repo.approve(first, approved_by="admin-test")
    row = (
        await session.execute(
            text("select name, is_approved from wb.negative_reasons where id=:id"),
            {"id": reason_id},
        )
    ).one()
    assert (row.name, row.is_approved) == ("Test cause", True)
    assert (
        await session.execute(
            text("select count(*) from wb.feedback_reasons where reason_id = :id"),
            {"id": reason_id},
        )
    ).scalar_one() == 4
    assert (
        await session.execute(
            text("""
                select count(*) from wb.feedback_reasons
                where feedback_id = any(cast(:feedback_ids as text[])) and reason_id = :fallback
            """),
            {"feedback_ids": source_ids[4:6], "fallback": fallback_id},
        )
    ).scalar_one() == 2
    candidates = await repo.candidates()
    assert any(item.id == rejected and item.status == "rejected" for item in candidates)
