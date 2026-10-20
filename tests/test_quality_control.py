"""Human approval of a blind, ordinal-keyed control sample."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import UTC, datetime
from io import StringIO
from zipfile import ZipFile

import pytest
import xlsxwriter
from app.services.quality import (
    combine_source_csv,
    import_approval_xlsx,
    make_blind_review_workbook,
)


def _csv(headers: list[str], rows: list[list[object]]) -> str:
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(rows)
    return output.getvalue()


def _inputs(count: int = 120) -> tuple[str, str]:
    reviews = _csv(
        ["ordinal", "feedback_id", "account_key", "stars", "created_date", "text_clean"],
        [
            [number, f"id-{number}", "default", 2, "DemoPhone", f"review body {number}"]
            for number in range(1, count + 1)
        ],
    )
    catalog = _csv(["reason_id", "reason_name"], [[1, "протекают"], [2, "впитываемость"]])
    return reviews, catalog


def _completed_workbook(
    *, count: int = 120, edited_id: str | None = None, omitted_ordinal: int | None = None
) -> bytes:
    # Model a completed copy of the workbook template saved from Excel.
    buffer = io.BytesIO()
    book = xlsxwriter.Workbook(buffer, {"in_memory": True})
    sheet = book.add_worksheet("Разметка")
    for col, value in enumerate(
        [
            "ordinal",
            "feedback_id",
            "cabinet",
            "stars",
            "review_text",
            "gold_reason_ids",
            "reviewer_note",
        ]
    ):
        sheet.write(0, col, value)
    for ordinal in range(1, count + 1):
        if ordinal == omitted_ordinal:
            continue
        row = ordinal
        sheet.write_number(row, 0, ordinal)
        sheet.write(row, 1, f"id-{ordinal}" if ordinal != 1 or edited_id is None else edited_id)
        sheet.write(row, 2, "default")
        sheet.write_number(row, 3, 2)
        sheet.write(row, 4, f"review body {ordinal}")
        sheet.write(row, 5, "1;2" if ordinal == 1 else "2")
        sheet.write(row, 6, "checked")
    book.close()
    return buffer.getvalue()


def test_blind_workbook_omits_ai_labels_and_has_all_reviews_and_catalog() -> None:
    reviews, catalog = _inputs()
    workbook = make_blind_review_workbook(reviews, catalog)

    with ZipFile(__import__("io").BytesIO(workbook)) as archive:
        sheet_xml = archive.read("xl/worksheets/sheet1.xml").decode()
        shared = archive.read("xl/sharedStrings.xml").decode()

    assert "ai_reason" not in sheet_xml + shared
    assert "gold_reason_ids" in sheet_xml + shared
    assert sheet_xml.count("<row ") == 123  # title, instructions, header, and 120 reviews
    assert "протекают" in shared
    assert "впитываемость" in shared


def test_blind_workbook_shows_human_proposals_and_requires_decisions() -> None:
    reviews, catalog = _inputs()
    proposals = _csv(
        ["ordinal", "feedback_id", "proposed_reason_ids", "annotation_note"],
        [[number, f"id-{number}", "2", "first pass"] for number in range(1, 121)],
    )
    workbook = make_blind_review_workbook(reviews, catalog, proposals_csv=proposals)
    with ZipFile(io.BytesIO(workbook)) as archive:
        strings = archive.read("xl/sharedStrings.xml").decode()
    assert "proposed_reason_ids" in strings
    assert "review_decision" in strings
    assert "ai_reason" not in strings

    with pytest.raises(ValueError, match="решение"):
        import_approval_xlsx(
            workbook,
            source_reviews_csv=reviews,
            catalog_csv=catalog,
            reviewer="operator",
            source_proposals_csv=proposals,
        )


def test_combine_source_csv_adds_missing_ordinals_and_drops_label_columns(tmp_path) -> None:
    reviews, _catalog = _inputs()
    records = list(csv.DictReader(StringIO(reviews)))
    first_path = tmp_path / "first.csv"
    extra_path = tmp_path / "extra.csv"
    first_path.write_text(
        _csv(
            [
                "feedback_id",
                "account_key",
                "stars",
                "created_date",
                "text_clean",
                "gold_reason_ids",
            ],
            [
                [
                    row["feedback_id"],
                    row["account_key"],
                    row["stars"],
                    row["created_date"],
                    row["text_clean"],
                    "999",
                ]
                for row in records[:100]
            ],
        ),
        encoding="utf-8",
    )
    extra_path.write_text(
        _csv(
            ["ordinal", "feedback_id", "account_key", "stars", "created_date", "text_clean"],
            [
                [
                    row["ordinal"],
                    row["feedback_id"],
                    row["account_key"],
                    row["stars"],
                    row["created_date"],
                    row["text_clean"],
                ]
                for row in records[100:]
            ],
        ),
        encoding="utf-8",
    )

    combined = list(csv.DictReader(StringIO(combine_source_csv([first_path, extra_path]))))

    assert len(combined) == 120
    assert combined[0]["ordinal"] == "1"
    assert combined[100]["ordinal"] == "101"
    assert combined[119]["feedback_id"] == "id-120"
    assert "gold_reason_ids" not in combined[0]


def test_approval_import_maps_ordinal_to_canonical_id_and_audits_versions() -> None:
    reviews, catalog = _inputs()
    reviewed_at = datetime(2026, 10, 15, 12, tzinfo=UTC)

    result = import_approval_xlsx(
        _completed_workbook(),
        source_reviews_csv=reviews,
        catalog_csv=catalog,
        reviewer="operator",
        reviewed_at=reviewed_at,
    )
    labels = list(csv.DictReader(StringIO(result.evaluation_csv)))
    audit = list(csv.DictReader(StringIO(result.audit_csv)))
    manifest = json.loads(result.manifest_json)

    assert len(labels) == len(audit) == 120
    assert labels[0]["feedback_id"] == "id-1"
    assert labels[0]["gold_reason_ids"] == "1;2"
    assert labels[-1]["feedback_id"] == "id-120"
    assert audit[0]["ordinal"] == "1"
    assert audit[0]["reviewer"] == "operator"
    assert manifest["source_reviews_sha256"] == hashlib.sha256(reviews.encode()).hexdigest()
    assert manifest["catalog_sha256"] == hashlib.sha256(catalog.encode()).hexdigest()
    assert manifest["reviewed_at"] == reviewed_at.isoformat()


def test_independent_100_review_sample_can_be_generated_and_approved(tmp_path) -> None:
    reviews, catalog = _inputs(100)
    sample_path = tmp_path / "sample_100.csv"
    sample_records = list(csv.DictReader(StringIO(reviews)))
    source_reviews = _csv(
        ["feedback_id", "account_key", "stars", "created_date", "text_clean"],
        [
            [
                row["feedback_id"],
                row["account_key"],
                row["stars"],
                row["created_date"],
                row["text_clean"],
            ]
            for row in sample_records
        ],
    )
    sample_path.write_text(source_reviews, encoding="utf-8")
    canonical_csv = combine_source_csv([sample_path])
    workbook = make_blind_review_workbook(canonical_csv, catalog)

    result = import_approval_xlsx(
        _completed_workbook(count=100),
        source_reviews_csv=canonical_csv,
        catalog_csv=catalog,
        reviewer="operator",
    )

    assert workbook.startswith(b"PK")
    assert len(result.reviews) == 100
    assert len(list(csv.DictReader(StringIO(result.evaluation_csv)))) == 100


def test_approval_import_rejects_missing_rows_and_wrong_ordinal_id_mapping() -> None:
    reviews, catalog = _inputs()

    with pytest.raises(ValueError, match="120"):
        import_approval_xlsx(
            _completed_workbook(omitted_ordinal=18),
            source_reviews_csv=reviews,
            catalog_csv=catalog,
            reviewer="operator",
        )

    with pytest.raises(ValueError, match="не совпадает"):
        import_approval_xlsx(
            _completed_workbook(edited_id="id-999"),
            source_reviews_csv=reviews,
            catalog_csv=catalog,
            reviewer="operator",
        )


def test_approval_import_rejects_unapproved_reasons() -> None:
    reviews, catalog = _inputs()
    buffer = io.BytesIO()
    book = xlsxwriter.Workbook(buffer, {"in_memory": True})
    sheet = book.add_worksheet("Разметка")
    headers = [
        "ordinal",
        "feedback_id",
        "cabinet",
        "stars",
        "review_text",
        "gold_reason_ids",
        "reviewer_note",
    ]
    for col, value in enumerate(headers):
        sheet.write(0, col, value)
    for ordinal in range(1, 121):
        row = ordinal
        values = [
            ordinal,
            f"id-{ordinal}",
            "default",
            2,
            f"review body {ordinal}",
            "999" if ordinal == 1 else "2",
            "checked",
        ]
        for col, value in enumerate(values):
            sheet.write(row, col, value)
    book.close()

    with pytest.raises(ValueError, match="Неизвестный ID причины: 999"):
        import_approval_xlsx(
            buffer.getvalue(),
            source_reviews_csv=reviews,
            catalog_csv=catalog,
            reviewer="operator",
        )
