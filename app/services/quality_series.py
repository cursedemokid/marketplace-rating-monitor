"""Read-only reliability scoring for a fixed AI verification series."""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import re
from dataclasses import dataclass
from io import StringIO

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.classify import sanitize


@dataclass(frozen=True, slots=True)
class SeriesResult:
    total: int
    first_attempt_success: int
    failed: int
    unattempted: int
    pass_gate: bool
    sample_sha256: str
    model: str
    prompt_sha256: str
    batch_size: int
    selected_at: dt.datetime


_SERIES_SIZE = 100
_GATE_SUCCESS_COUNT = 95
_PROMPT_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _parse_sample(content: str) -> list[dict[str, str]]:
    reader = csv.DictReader(StringIO(content.removeprefix("\ufeff")))
    required = {"feedback_id", "stars", "text_clean"}
    if reader.fieldnames is None or not required <= set(reader.fieldnames):
        raise ValueError(
            "CSV контрольной серии должен содержать колонки feedback_id, stars и text_clean"
        )

    rows = [
        {str(key): str(value or "") for key, value in row.items() if key is not None}
        for row in reader
    ]
    if len(rows) != _SERIES_SIZE:
        raise ValueError("В серии должно быть ровно 100 уникальных отзывов")

    feedback_ids: set[str] = set()
    for row in rows:
        feedback_id = row["feedback_id"].strip()
        if not feedback_id:
            raise ValueError("В контрольной серии обнаружен пустой feedback_id")
        if feedback_id in feedback_ids:
            raise ValueError(f"feedback_id повторяется: {feedback_id}")
        feedback_ids.add(feedback_id)

        try:
            stars = int(row["stars"])
        except ValueError as exc:
            raise ValueError(f"Некорректная оценка для отзыва {feedback_id}") from exc
        if not 1 <= stars <= 3:
            raise ValueError("В выборке должны быть только негативные отзывы с оценкой 1–3")
        if not row["text_clean"].strip():
            raise ValueError("В выборке должны быть отзывы с непустым текстом")
        row["feedback_id"] = feedback_id

    return rows


async def verify_series(
    session: AsyncSession,
    sample_csv: str,
    *,
    selected_at: dt.datetime,
    model: str,
    prompt_sha256: str,
    batch_size: int,
) -> SeriesResult:
    """Score first-attempt successes without changing predictions or cost records.

    A retry is never counted as a first-attempt success, even if it produced
    labels. Reviews with no claim created at or after ``selected_at`` are
    unattempted and count against the 95-of-100 gate.
    """
    if selected_at.tzinfo is None or selected_at.utcoffset() is None:
        raise ValueError("selected_at должен быть timezone-aware")
    if not model.strip():
        raise ValueError("Нужно указать модель контрольной серии")
    if not _PROMPT_SHA256.fullmatch(prompt_sha256):
        raise ValueError("prompt_sha256 должен содержать 64 строчные hex-цифры")
    if batch_size < 1:
        raise ValueError("batch_size должен быть положительным")

    sample = _parse_sample(sample_csv)
    feedback_ids = [row["feedback_id"] for row in sample]
    records = (
        (
            await session.execute(
                text("""
                with selected as (
                    select unnest(cast(:feedback_ids as text[])) as feedback_id
                )
                select selected.feedback_id,
                       f.valuation as stars,
                       concat_ws(E'\n', nullif(f.text, ''), nullif(f.pros, ''),
                                 nullif(f.cons, '')) as body,
                       c.claimed_at,
                       request.model,
                       request.classifier_prompt_sha256,
                       request.classifier_batch_size,
                       request.status as request_status,
                       exists (
                           select 1 from wb.feedback_reasons fr
                           where fr.feedback_id = selected.feedback_id
                       ) as has_labels,
                       exists (
                           select 1 from wb.ai_review_retries retry
                           where retry.feedback_id = selected.feedback_id
                             and retry.previous_request_id = c.request_id
                       ) as was_retried
                  from selected
                  left join wb.feedbacks f on f.id = selected.feedback_id
                  left join wb.ai_review_claims c on c.feedback_id = selected.feedback_id
                  left join wb.ai_cost_requests request on request.id = c.request_id
            """),
                {"feedback_ids": feedback_ids},
            )
        )
        .mappings()
        .all()
    )

    by_id = {str(row["feedback_id"]): row for row in records}
    if set(by_id) != set(feedback_ids):
        raise ValueError("Не удалось сопоставить контрольную выборку с отзывами в базе")

    first_attempt_success = 0
    failed = 0
    unattempted = 0
    for row in sample:
        feedback_id = row["feedback_id"]
        record = by_id[feedback_id]
        try:
            database_stars = int(record["stars"])
            database_text = str(record["body"] or "").strip()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Отзыв {feedback_id} отсутствует в базе") from exc
        if not 1 <= database_stars <= 3 or not database_text:
            raise ValueError(f"Отзыв {feedback_id} в базе не является текстовым негативным отзывом")
        if int(row["stars"]) != database_stars or sanitize(row["text_clean"]) != sanitize(
            database_text
        ):
            raise ValueError(f"Данные отзыва {feedback_id} в CSV не совпадают с базой")

        claimed_at = record["claimed_at"]
        if claimed_at is None or claimed_at < selected_at:
            unattempted += 1
            continue
        if record["model"] != model:
            raise ValueError(
                f"Для отзыва {feedback_id} использована модель {record['model']!r}, "
                f"ожидалась {model!r}"
            )
        if record["classifier_prompt_sha256"] != prompt_sha256:
            raise ValueError(f"Для отзыва {feedback_id} использован другой промпт")
        if record["classifier_batch_size"] != batch_size:
            raise ValueError(f"Для отзыва {feedback_id} использован другой размер пакета")

        success = (
            record["request_status"] == "settled"
            and bool(record["has_labels"])
            and not bool(record["was_retried"])
        )
        if success:
            first_attempt_success += 1
        else:
            failed += 1

    total = len(sample)
    return SeriesResult(
        total=total,
        first_attempt_success=first_attempt_success,
        failed=failed,
        unattempted=unattempted,
        pass_gate=first_attempt_success >= _GATE_SUCCESS_COUNT,
        sample_sha256=hashlib.sha256(sample_csv.encode("utf-8")).hexdigest(),
        model=model,
        prompt_sha256=prompt_sha256,
        batch_size=batch_size,
        selected_at=selected_at,
    )
