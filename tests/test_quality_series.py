"""First-attempt reliability gate for a fixed 100-review AI series."""

import csv
import datetime as dt
import hashlib
from io import StringIO

import pytest
from app.services.quality_series import verify_series


def _series_csv(count: int = 100) -> str:
    output = StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=("ordinal", "feedback_id", "stars", "text_clean"),
    )
    writer.writeheader()
    for index in range(count):
        writer.writerow(
            {
                "ordinal": index + 1,
                "feedback_id": f"series-{index}",
                "stars": 1,
                "text_clean": f"Отзыв {index}",
            }
        )
    return output.getvalue()


class _Result:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> "_Result":
        return self

    def all(self) -> list[dict[str, object]]:
        return self._rows


class _Session:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.params: dict[str, object] | None = None

    async def execute(self, query: object, params: dict[str, object]) -> _Result:
        self.params = params
        return _Result(self.rows)


def _db_rows(
    *,
    successful: int = 95,
    failed: int = 3,
    unattempted: int = 2,
    retry_successes: int = 0,
    prompt_sha256: str = "a" * 64,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    selected_at = dt.datetime(2026, 10, 15, tzinfo=dt.UTC)
    for index in range(100):
        if index < successful:
            claim_at: dt.datetime | None = selected_at + dt.timedelta(seconds=2)
            has_labels = True
            retried = index < retry_successes
            status = "settled"
        elif index < successful + failed:
            claim_at = selected_at + dt.timedelta(seconds=2)
            has_labels = False
            retried = False
            status = "uncertain"
        else:
            claim_at = None
            has_labels = False
            retried = False
            status = None
        rows.append(
            {
                "feedback_id": f"series-{index}",
                "stars": 1,
                "body": f"Отзыв {index}",
                "claimed_at": claim_at,
                "model": "test-model" if claim_at else None,
                "classifier_prompt_sha256": prompt_sha256 if claim_at else None,
                "classifier_batch_size": 30 if claim_at else None,
                "request_status": status,
                "has_labels": has_labels,
                "was_retried": retried,
            }
        )
    return rows


async def test_verify_series_counts_unattempted_and_retry_as_first_attempt_failures() -> None:
    csv_content = _series_csv()
    selected_at = dt.datetime(2026, 10, 15, tzinfo=dt.UTC)
    session = _Session(_db_rows(successful=95, failed=3, unattempted=2, retry_successes=1))

    result = await verify_series(
        session,  # type: ignore[arg-type]
        csv_content,
        selected_at=selected_at,
        model="test-model",
        prompt_sha256="a" * 64,
        batch_size=30,
    )

    assert result.total == 100
    assert result.first_attempt_success == 94
    assert result.failed == 4
    assert result.unattempted == 2
    assert result.pass_gate is False
    assert result.sample_sha256 == hashlib.sha256(csv_content.encode()).hexdigest()
    assert session.params == {"feedback_ids": [f"series-{index}" for index in range(100)]}


async def test_verify_series_passes_at_95_first_attempt_successes() -> None:
    result = await verify_series(
        _Session(_db_rows(successful=95, failed=5, prompt_sha256="b" * 64)),  # type: ignore[arg-type]
        _series_csv(),
        selected_at=dt.datetime(2026, 10, 15, tzinfo=dt.UTC),
        model="test-model",
        prompt_sha256="b" * 64,
        batch_size=30,
    )

    assert result.first_attempt_success == 95
    assert result.pass_gate is True


async def test_verify_series_rejects_different_prompt_or_batch_size() -> None:
    rows = _db_rows()
    rows[0]["classifier_batch_size"] = 20
    with pytest.raises(ValueError, match="другой размер пакета"):
        await verify_series(
            _Session(rows),  # type: ignore[arg-type]
            _series_csv(),
            selected_at=dt.datetime(2026, 10, 15, tzinfo=dt.UTC),
            model="test-model",
            prompt_sha256="a" * 64,
            batch_size=30,
        )


async def test_verify_series_rejects_csv_review_text_that_does_not_match_database() -> None:
    with pytest.raises(ValueError, match="не совпадают с базой"):
        await verify_series(
            _Session(_db_rows()),  # type: ignore[arg-type]
            _series_csv().replace("Отзыв 0", "Подменённый текст"),
            selected_at=dt.datetime(2026, 10, 15, tzinfo=dt.UTC),
            model="test-model",
            prompt_sha256="b" * 64,
            batch_size=30,
        )


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (_series_csv(99), "100 уникальных отзывов"),
        (_series_csv(101), "100 уникальных отзывов"),
    ],
)
async def test_verify_series_requires_exactly_100_rows(content: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        await verify_series(
            _Session([]),  # type: ignore[arg-type]
            content,
            selected_at=dt.datetime(2026, 10, 15, tzinfo=dt.UTC),
            model="test-model",
            prompt_sha256="c" * 64,
            batch_size=30,
        )


async def test_verify_series_rejects_duplicate_ids() -> None:
    content = _series_csv().replace("series-99", "series-98")
    with pytest.raises(ValueError, match="повторяется"):
        await verify_series(
            _Session([]),  # type: ignore[arg-type]
            content,
            selected_at=dt.datetime(2026, 10, 15, tzinfo=dt.UTC),
            model="test-model",
            prompt_sha256="d" * 64,
            batch_size=30,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("stars", "4", "негативные отзывы"),
        ("text_clean", "   ", "непустым текстом"),
    ],
)
async def test_verify_series_rejects_rows_not_suitable_for_negative_text_sample(
    field: str, value: str, message: str
) -> None:
    content = _series_csv().replace("series-0,1,Отзыв 0", f"series-0,{value},Отзыв 0")
    if field == "text_clean":
        content = _series_csv().replace("Отзыв 0", "   ")
    with pytest.raises(ValueError, match=message):
        await verify_series(
            _Session([]),  # type: ignore[arg-type]
            content,
            selected_at=dt.datetime(2026, 10, 15, tzinfo=dt.UTC),
            model="test-model",
            prompt_sha256="e" * 64,
            batch_size=30,
        )


async def test_verify_series_requires_timezone_aware_selection_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        await verify_series(
            _Session([]),  # type: ignore[arg-type]
            _series_csv(),
            selected_at=dt.datetime(2026, 10, 15),  # noqa: DTZ001 - invalid input under test
            model="test-model",
            prompt_sha256="f" * 64,
            batch_size=30,
        )
