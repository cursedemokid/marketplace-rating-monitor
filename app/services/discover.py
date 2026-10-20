"""Локальный поиск повторяющихся тем в неразобранных негативных отзывах."""

from __future__ import annotations

import datetime as dt
import inspect
import os
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.discovery import DiscoveryRepository, DiscoveryReview

EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
EMBEDDING_REVISION = "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"


class Embedder(Protocol):
    def encode(self, sentences: list[str], **kwargs: Any) -> Any: ...


class Clusterer(Protocol):
    def fit_predict(self, embeddings: Any) -> Sequence[int]: ...


NameProposer = Callable[[Sequence[str]], str | Awaitable[str] | None]


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    examined_reviews: int
    created_candidates: tuple[int, ...]


def _local_embedder() -> Embedder:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError(
            "Для discover установите sentence-transformers и заранее скачайте модель "
            f"{EMBEDDING_MODEL} в локальный кеш"
        ) from exc
    configured_path = os.environ.get("WB_EMBEDDING_MODEL_PATH")
    if configured_path:
        model_path = Path(configured_path)
        if not model_path.is_dir():
            raise RuntimeError(f"Локальная модель MiniLM не найдена: {model_path}")
        return cast(
            Embedder,
            SentenceTransformer(str(model_path), device="cpu", local_files_only=True),
        )
    return cast(
        Embedder,
        SentenceTransformer(
            EMBEDDING_MODEL,
            revision=EMBEDDING_REVISION,
            device="cpu",
            local_files_only=True,
        ),
    )


def _hdbscan(min_cluster_size: int) -> Clusterer:
    try:
        from sklearn.cluster import HDBSCAN
    except ImportError as exc:
        raise RuntimeError("Установите scikit-learn с поддержкой HDBSCAN") from exc
    return cast(Clusterer, HDBSCAN(min_cluster_size=min_cluster_size, metric="euclidean"))


def cluster_reviews(
    reviews: Sequence[DiscoveryReview],
    *,
    embedder: Embedder,
    clusterer: Clusterer,
    min_cluster_size: int,
) -> list[list[str]]:
    """Group similar reviews; HDBSCAN noise points are deliberately discarded."""
    if min_cluster_size < 2:
        raise ValueError("Размер кластера должен быть не меньше двух отзывов")
    if len(reviews) < min_cluster_size:
        return []
    vectors = embedder.encode(
        [review.body for review in reviews],
        convert_to_numpy=True,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    labels = clusterer.fit_predict(vectors)
    if len(labels) != len(reviews):
        raise ValueError("Кластеризатор вернул неверное число меток")
    grouped: dict[int, list[str]] = defaultdict(list)
    for review, label in zip(reviews, labels, strict=True):
        if int(label) >= 0:
            grouped[int(label)].append(review.feedback_id)
    return [ids for _label, ids in sorted(grouped.items()) if len(ids) >= min_cluster_size]


async def run(
    session: AsyncSession,
    *,
    until: dt.date,
    min_cluster_size: int = 3,
    batch_limit: int = 5000,
    embedder: Embedder | None = None,
    clusterer: Clusterer | None = None,
    name_proposer: NameProposer | None = None,
) -> DiscoveryResult:
    """Create pending candidates only; naming is opt-in and receives review text only."""
    if min_cluster_size < 2:
        raise ValueError("Размер кластера должен быть не меньше двух отзывов")
    if batch_limit < min_cluster_size:
        raise ValueError("Лимит отзывов должен быть не меньше минимального размера кластера")
    repo = DiscoveryRepository(session)
    reviews = await repo.repeated_unassigned(until=until, limit=batch_limit)
    if len(reviews) < min_cluster_size:
        await repo.mark_examined([review.feedback_id for review in reviews])
        return DiscoveryResult(len(reviews), ())
    embedder = embedder or _local_embedder()
    clusterer = clusterer or _hdbscan(min_cluster_size)
    groups = cluster_reviews(
        reviews,
        embedder=embedder,
        clusterer=clusterer,
        min_cluster_size=min_cluster_size,
    )
    names: list[str | None] = [None] * len(groups)
    if name_proposer is not None:
        review_by_id = {review.feedback_id: review for review in reviews}
        for index, group in enumerate(groups):
            proposed = name_proposer([review_by_id[item].body for item in group])
            if inspect.isawaitable(proposed):
                proposed = await proposed
            names[index] = str(proposed).strip() or None if proposed is not None else None
    candidate_ids = await repo.create_candidates(groups, names=names)
    await repo.mark_examined([review.feedback_id for review in reviews])
    logger.info(
        "Поиск причин: отзывов {}, найдено кандидатов {}",
        len(reviews),
        len(candidate_ids),
    )
    return DiscoveryResult(len(reviews), tuple(candidate_ids))
