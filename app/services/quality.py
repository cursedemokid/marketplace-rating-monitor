"""Воспроизводимая ручная разметка и оценка многометочной классификации."""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from io import BytesIO, StringIO
from pathlib import Path
from xml.etree import ElementTree

import xlsxwriter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.classify import sanitize


@dataclass(frozen=True, slots=True)
class QualityMetrics:
    reviews: int
    precision: float
    recall: float
    f1: float
    exact_match: float
    true_positive: int
    false_positive: int
    false_negative: int


@dataclass(frozen=True, slots=True)
class ApprovedReview:
    ordinal: int
    feedback_id: str
    gold_reason_ids: tuple[int, ...]
    reviewer_note: str


@dataclass(frozen=True, slots=True)
class QualityApproval:
    """Validated labels and immutable audit artifacts; does not mutate predictions or DB."""

    reviews: tuple[ApprovedReview, ...]
    evaluation_csv: str
    audit_csv: str
    manifest_json: str


_REVIEW_HEADERS = (
    "ordinal",
    "feedback_id",
    "cabinet",
    "stars",
    "review_text",
    "gold_reason_ids",
    "reviewer_note",
)
_MIN_QUALITY_SAMPLE_SIZE = 100
_XLSX_NS = {
    "main": "http://service.example.invalid/spreadsheetml/2006/main",
    "rel": "http://service.example.invalid/officeDocument/2006/relationships",
    "pkg": "http://service.example.invalid/package/2006/relationships",
}


def _read_csv_records(
    content: str, *, required: set[str], description: str
) -> list[dict[str, str]]:
    reader = csv.DictReader(StringIO(content.removeprefix("\ufeff")))
    if reader.fieldnames is None or not required <= set(reader.fieldnames):
        raise ValueError(f"{description}: отсутствуют колонки {', '.join(sorted(required))}")
    return [
        {str(key): str(value or "") for key, value in row.items() if key is not None}
        for row in reader
    ]


def combine_source_csv(paths: list[Path]) -> str:
    """Combine the control sample's source CSVs without carrying over any labels."""
    if not paths:
        raise ValueError("Нужны исходные CSV контрольной выборки")
    required = {"feedback_id", "account_key", "stars", "created_date", "text_clean"}
    records: list[dict[str, str]] = []
    for path_index, path in enumerate(paths):
        source = path.read_text(encoding="utf-8-sig")
        source_records = _read_csv_records(
            source,
            required=required,
            description=f"CSV отзывов {path}",
        )
        if path_index and source_records and "ordinal" not in source_records[0]:
            raise ValueError(f"В дополнительном CSV не указан порядковый номер: {path}")
        for row_index, record in enumerate(source_records, start=1):
            if not record.get("ordinal", "").strip():
                if path_index:
                    raise ValueError(f"В дополнительном CSV не указан порядковый номер: {path}")
                record["ordinal"] = str(row_index)
            records.append(record)
    if len(records) < _MIN_QUALITY_SAMPLE_SIZE:
        raise ValueError(
            f"В контрольной выборке нужно не меньше {_MIN_QUALITY_SAMPLE_SIZE} строк, "
            f"найдено {len(records)}"
        )
    by_ordinal = _validate_canonical_ordinals(records)
    fields = ["ordinal", "feedback_id", "account_key", "stars", "created_date", "text_clean"]
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(
        {field: record.get(field, "") for field in fields}
        for _ordinal, record in sorted(by_ordinal.items())
    )
    return output.getvalue()


def combine_proposals_csv(paths: list[Path]) -> str:
    """Join first-pass human proposals by the stable Excel ordinal."""
    records: list[dict[str, str]] = []
    for path in paths:
        records.extend(
            _read_csv_records(
                path.read_text(encoding="utf-8-sig"),
                required={"ordinal", "feedback_id", "proposed_reason_ids"},
                description=f"Предварительная разметка {path}",
            )
        )
    ordered = _validate_canonical_ordinals(records)
    output = StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["ordinal", "feedback_id", "proposed_reason_ids", "annotation_note"],
    )
    writer.writeheader()
    writer.writerows(
        {field: record.get(field, "") for field in writer.fieldnames}
        for _ordinal, record in sorted(ordered.items())
    )
    return output.getvalue()


def _validated_proposals(
    proposals_csv: str, reviews: dict[int, dict[str, str]], allowed_ids: set[int]
) -> dict[int, dict[str, str]]:
    proposals = _validate_canonical_ordinals(
        _read_csv_records(
            proposals_csv,
            required={"ordinal", "feedback_id", "proposed_reason_ids"},
            description="Предварительная разметка",
        )
    )
    if set(proposals) != set(reviews):
        raise ValueError("Предварительная разметка не покрывает все отзывы")
    for ordinal, proposal in proposals.items():
        if proposal["feedback_id"] != reviews[ordinal]["feedback_id"]:
            raise ValueError(f"ID отзыва в предварительной разметке №{ordinal} не совпадает")
        _parse_reason_ids(proposal["proposed_reason_ids"], allowed=allowed_ids)
    return proposals


def make_blind_review_workbook(
    source_reviews_csv: str, catalog_csv: str, *, proposals_csv: str | None = None
) -> bytes:
    """Create an ordinal-keyed review workbook without exposing AI predictions."""
    reviews = _read_csv_records(
        source_reviews_csv,
        required={"ordinal", "feedback_id", "account_key", "stars", "text_clean"},
        description="CSV отзывов",
    )
    if len(reviews) < _MIN_QUALITY_SAMPLE_SIZE:
        raise ValueError(
            f"Для контрольной книги нужно не меньше {_MIN_QUALITY_SAMPLE_SIZE} отзывов, "
            f"получено {len(reviews)}"
        )
    canonical = _validate_canonical_ordinals(reviews)
    reasons = _read_csv_records(
        catalog_csv,
        required={"reason_id", "reason_name"},
        description="Справочник причин",
    )
    allowed_ids = _catalog_ids(reasons)
    proposals = (
        _validated_proposals(proposals_csv, canonical, allowed_ids)
        if proposals_csv is not None
        else None
    )

    output = BytesIO()
    book = xlsxwriter.Workbook(
        output,
        {"in_memory": True, "strings_to_formulas": False, "strings_to_urls": False},
    )
    sheet = book.add_worksheet("Разметка")
    catalog_sheet = book.add_worksheet("Причины")
    title = book.add_format({"bold": True, "font_size": 16, "font_color": "#183153"})
    note = book.add_format({"text_wrap": True, "font_color": "#506175", "valign": "top"})
    header = book.add_format(
        {"bold": True, "font_color": "#FFFFFF", "bg_color": "#183153", "text_wrap": True}
    )
    ordinal_format = book.add_format({"align": "center", "num_format": "0"})
    star_format = book.add_format({"align": "center", "num_format": "0"})
    decision_format = book.add_format({"bg_color": "#FFF2CC", "text_wrap": True, "valign": "top"})
    body_format = book.add_format({"text_wrap": True, "valign": "top"})

    headers = (
        (
            "ordinal",
            "feedback_id",
            "cabinet",
            "stars",
            "review_text",
            "proposed_reason_ids",
            "gold_reason_ids",
            "review_decision",
            "reviewer_note",
        )
        if proposals is not None
        else _REVIEW_HEADERS
    )
    last_column = "I" if proposals is not None else "G"
    sheet.merge_range(
        f"A1:{last_column}1", f"Контрольная разметка отзывов — {len(reviews)} строк", title
    )
    instructions = (
        "Проверьте предложенные причины: исправьте gold_reason_ids при необходимости и "
        "укажите review_decision = подтверждаю или исправлено. Порядковый номер и ID не "
        "меняйте. Ответы AI скрыты."
        if proposals is not None
        else "Для каждой строки заполните gold_reason_ids: один или несколько ID "
        "через точку с запятой. "
        "Используйте только ID со вкладки «Причины». Порядковый номер и ID отзыва не меняйте. "
        "Причины AI скрыты."
    )
    sheet.merge_range(
        f"A2:{last_column}2",
        instructions,
        note,
    )
    sheet.set_row(1, 42)
    header_row = 3
    for column, value in enumerate(headers):
        sheet.write_string(header_row, column, value, header)
    widths = (
        [10, 26, 16, 9, 76, 24, 24, 18, 42]
        if proposals is not None
        else [10, 26, 16, 9, 76, 24, 42]
    )
    for column, width in enumerate(widths):
        sheet.set_column(column, column, width)
    for offset, review in enumerate(reviews, start=header_row + 1):
        ordinal = int(review["ordinal"])
        sheet.write_number(offset, 0, ordinal, ordinal_format)
        sheet.write_string(offset, 1, review["feedback_id"], body_format)
        sheet.write_string(offset, 2, review["account_key"], body_format)
        sheet.write_number(offset, 3, int(review["stars"]), star_format)
        sheet.write_string(offset, 4, review["text_clean"], body_format)
        if proposals is not None:
            proposal = proposals[ordinal]
            sheet.write_string(offset, 5, proposal["proposed_reason_ids"], body_format)
            sheet.write_string(offset, 6, proposal["proposed_reason_ids"], decision_format)
            sheet.write_blank(offset, 7, None, decision_format)
            sheet.write_string(offset, 8, proposal.get("annotation_note", ""), decision_format)
        else:
            sheet.write_blank(offset, 5, None, decision_format)
            sheet.write_blank(offset, 6, None, decision_format)
        sheet.set_row(offset, 45)
    sheet.freeze_panes(header_row + 1, 2)
    sheet.autofilter(header_row, 0, header_row + len(reviews), len(headers) - 1)

    catalog_sheet.write_string(0, 0, "ID причины", header)
    catalog_sheet.write_string(0, 1, "Причина", header)
    catalog_sheet.set_column(0, 0, 14)
    catalog_sheet.set_column(1, 1, 48)
    for row_index, reason in enumerate(reasons, start=1):
        catalog_sheet.write_number(row_index, 0, int(reason["reason_id"]))
        catalog_sheet.write_string(row_index, 1, reason["reason_name"])
    catalog_sheet.freeze_panes(1, 0)
    book.close()
    return output.getvalue()


def _column_number(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference)
    if letters is None:
        raise ValueError(f"Некорректная ссылка XLSX: {reference}")
    number = 0
    for char in letters.group():
        number = number * 26 + ord(char) - ord("A") + 1
    return number - 1


def _read_review_sheet(content: bytes) -> list[list[str]]:
    """Read cell values from the reviewer worksheet using only the XLSX standard library."""
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
            relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            targets = {
                relation.attrib["Id"]: relation.attrib["Target"]
                for relation in relationships.findall("pkg:Relationship", _XLSX_NS)
            }
            sheet_path = None
            for sheet in workbook.findall("main:sheets/main:sheet", _XLSX_NS):
                if sheet.attrib.get("name") == "Разметка":
                    relation_id = sheet.attrib[f"{{{_XLSX_NS['rel']}}}id"]
                    target = targets[relation_id].lstrip("/")
                    sheet_path = target if target.startswith("xl/") else f"xl/{target}"
                    break
            if sheet_path is None:
                raise ValueError("В XLSX нет вкладки «Разметка»")
            shared_strings: list[str] = []
            if "xl/sharedStrings.xml" in archive.namelist():
                strings = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
                shared_strings = [
                    "".join(
                        text_node.text or "" for text_node in item.findall(".//main:t", _XLSX_NS)
                    )
                    for item in strings.findall("main:si", _XLSX_NS)
                ]
            sheet_root = ElementTree.fromstring(archive.read(sheet_path))
    except (KeyError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise ValueError("Не удалось прочитать книгу XLSX с разметкой") from exc

    rows: list[list[str]] = []
    for row_element in sheet_root.findall("main:sheetData/main:row", _XLSX_NS):
        cells: dict[int, str] = {}
        for cell in row_element.findall("main:c", _XLSX_NS):
            column = _column_number(cell.attrib["r"])
            cell_type = cell.attrib.get("t")
            if cell_type == "inlineStr":
                value = "".join(
                    text_node.text or "" for text_node in cell.findall(".//main:t", _XLSX_NS)
                )
            else:
                value_element = cell.find("main:v", _XLSX_NS)
                value = value_element.text or "" if value_element is not None else ""
                if cell_type == "s" and value:
                    value = shared_strings[int(value)]
            cells[column] = value or ""
        rows.append([cells.get(index, "") for index in range(max(cells, default=-1) + 1)])
    return rows


def _validate_canonical_ordinals(records: list[dict[str, str]]) -> dict[int, dict[str, str]]:
    by_ordinal: dict[int, dict[str, str]] = {}
    for record in records:
        try:
            ordinal = int(record["ordinal"])
        except (KeyError, ValueError) as exc:
            raise ValueError("Порядковые номера должны быть целыми числами") from exc
        if ordinal in by_ordinal or not record.get("feedback_id", "").strip():
            raise ValueError("Порядковые номера и feedback_id должны быть заполнены и уникальны")
        by_ordinal[ordinal] = record
    expected = set(range(1, len(records) + 1))
    if set(by_ordinal) != expected:
        raise ValueError("Порядковые номера должны идти подряд с 1 без пропусков")
    if len({record["feedback_id"] for record in records}) != len(records):
        raise ValueError("feedback_id должны быть уникальны")
    return by_ordinal


def _catalog_ids(records: list[dict[str, str]]) -> set[int]:
    allowed: set[int] = set()
    for record in records:
        try:
            reason_id = int(record["reason_id"])
        except (KeyError, ValueError) as exc:
            raise ValueError("Справочник содержит некорректный ID причины") from exc
        if reason_id in allowed:
            raise ValueError(f"ID причины повторяется в справочнике: {reason_id}")
        allowed.add(reason_id)
    if not allowed:
        raise ValueError("Справочник причин пуст")
    return allowed


def import_approval_xlsx(
    content: bytes,
    *,
    source_reviews_csv: str,
    catalog_csv: str,
    reviewer: str,
    reviewed_at: dt.datetime | None = None,
    classifier_model: str | None = None,
    source_proposals_csv: str | None = None,
) -> QualityApproval:
    """Validate all ordinal decisions and return versioned gold/audit artifacts."""
    if not reviewer.strip():
        raise ValueError("Нужно указать имя проверяющего")
    approved_at = reviewed_at or dt.datetime.now(dt.UTC)
    if approved_at.tzinfo is None or approved_at.utcoffset() is None:
        raise ValueError("Время подтверждения должно содержать часовой пояс")
    source_rows = _read_csv_records(
        source_reviews_csv,
        required={"ordinal", "feedback_id", "account_key", "stars", "text_clean"},
        description="CSV отзывов",
    )
    canonical = _validate_canonical_ordinals(source_rows)
    if len(canonical) < _MIN_QUALITY_SAMPLE_SIZE:
        raise ValueError(
            f"Для утверждения нужно не меньше {_MIN_QUALITY_SAMPLE_SIZE} отзывов, "
            f"получено {len(canonical)}"
        )
    catalog_rows = _read_csv_records(
        catalog_csv,
        required={"reason_id", "reason_name"},
        description="Справочник причин",
    )
    allowed_ids = _catalog_ids(catalog_rows)
    proposals = (
        _validated_proposals(source_proposals_csv, canonical, allowed_ids)
        if source_proposals_csv is not None
        else None
    )
    rows = _read_review_sheet(content)
    header_row_index = next(
        (index for index, row in enumerate(rows) if set(_REVIEW_HEADERS) <= set(row)),
        None,
    )
    if header_row_index is None:
        raise ValueError("В XLSX не найдены колонки разметки контрольной выборки")
    headers = rows[header_row_index]
    column_indexes = {name: headers.index(name) for name in _REVIEW_HEADERS}
    has_proposals = "proposed_reason_ids" in headers
    if proposals is not None and (not has_proposals or "review_decision" not in headers):
        raise ValueError("В книге отсутствуют предложения или решения по строкам")
    if has_proposals and "review_decision" not in headers:
        raise ValueError("В книге отсутствует колонка review_decision")
    if has_proposals:
        column_indexes["proposed_reason_ids"] = headers.index("proposed_reason_ids")
        column_indexes["review_decision"] = headers.index("review_decision")
    decisions: dict[int, ApprovedReview] = {}
    decision_details: dict[int, tuple[str, str]] = {}
    for row in rows[header_row_index + 1 :]:
        if not any(value.strip() for value in row):
            continue
        try:
            ordinal_value = row[column_indexes["ordinal"]].strip()
            ordinal = int(ordinal_value)
            if ordinal_value != str(ordinal):
                raise ValueError
        except (IndexError, ValueError) as exc:
            raise ValueError(
                "Каждая заполненная строка должна иметь целый порядковый номер"
            ) from exc
        if ordinal in decisions:
            raise ValueError(f"Порядковый номер повторяется: {ordinal}")
        expected = canonical.get(ordinal)
        if expected is None:
            raise ValueError(f"Неизвестный порядковый номер: {ordinal}")

        def cell(row_values: list[str], name: str) -> str:
            index = column_indexes[name]
            return row_values[index].strip() if index < len(row_values) else ""

        if cell(row, "feedback_id") != expected["feedback_id"]:
            raise ValueError(f"Для строки {ordinal} ID отзыва не совпадает с исходной выборкой")
        immutable_values = {
            "cabinet": expected["account_key"],
            "stars": expected["stars"],
            "review_text": expected["text_clean"],
        }
        for column, expected_value in immutable_values.items():
            if cell(row, column) != expected_value:
                raise ValueError(f"Для строки {ordinal} изменено исходное поле {column}")
        reason_ids = _parse_reason_ids(cell(row, "gold_reason_ids"), allowed=allowed_ids)
        if has_proposals:
            proposed = cell(row, "proposed_reason_ids")
            if proposals is not None and proposed != proposals[ordinal]["proposed_reason_ids"]:
                raise ValueError(f"Для строки {ordinal} изменены предложенные причины")
            _parse_reason_ids(proposed, allowed=allowed_ids)
            review_decision = cell(row, "review_decision").casefold()
            if review_decision not in {"подтверждаю", "исправлено"}:
                raise ValueError(
                    f"Для строки {ordinal} нужно указать решение: подтверждаю или исправлено"
                )
            if review_decision == "подтверждаю" and reason_ids != _parse_reason_ids(
                proposed, allowed=allowed_ids
            ):
                raise ValueError(f"Для строки {ordinal} изменены причины без решения «исправлено»")
            decision_details[ordinal] = (proposed, review_decision)
        decisions[ordinal] = ApprovedReview(
            ordinal,
            expected["feedback_id"],
            tuple(sorted(reason_ids)),
            cell(row, "reviewer_note"),
        )
    missing = sorted(set(canonical) - set(decisions))
    if missing:
        raise ValueError(
            f"Нужно подтвердить все {len(canonical)} строк; отсутствуют номера: {missing[:10]}"
        )

    from app.services.ai_cost import CLASSIFIER_SYSTEM_PROMPT

    prompt_hash = hashlib.sha256(CLASSIFIER_SYSTEM_PROMPT.encode("utf-8")).hexdigest()
    catalog_hash = hashlib.sha256(catalog_csv.encode("utf-8")).hexdigest()
    sample_hash = hashlib.sha256(source_reviews_csv.encode("utf-8")).hexdigest()
    workbook_hash = hashlib.sha256(content).hexdigest()
    reviewed_iso = approved_at.isoformat()
    evaluation = StringIO()
    eval_writer = csv.writer(evaluation)
    eval_writer.writerow(["feedback_id", "gold_reason_ids"])
    audit = StringIO()
    audit_writer = csv.writer(audit)
    audit_writer.writerow(
        [
            "ordinal",
            "feedback_id",
            "proposed_reason_ids",
            "gold_reason_ids",
            "review_decision",
            "reviewer_note",
            "reviewer",
            "reviewed_at",
        ]
    )
    for ordinal in sorted(decisions):
        decision = decisions[ordinal]
        reason_ids_csv = ";".join(map(str, decision.gold_reason_ids))
        eval_writer.writerow([decision.feedback_id, reason_ids_csv])
        audit_writer.writerow(
            [
                decision.ordinal,
                decision.feedback_id,
                decision_details.get(ordinal, ("", ""))[0],
                reason_ids_csv,
                decision_details.get(ordinal, ("", ""))[1],
                decision.reviewer_note,
                reviewer.strip(),
                reviewed_iso,
            ]
        )
    manifest = {
        "schema_version": 1,
        "sample_size": len(decisions),
        "reviewer": reviewer.strip(),
        "reviewed_at": reviewed_iso,
        "classifier_model": classifier_model,
        "prompt_sha256": prompt_hash,
        "catalog_sha256": catalog_hash,
        "source_reviews_sha256": sample_hash,
        "reviewer_workbook_sha256": workbook_hash,
        "proposals_sha256": (
            hashlib.sha256(source_proposals_csv.encode("utf-8")).hexdigest()
            if source_proposals_csv is not None
            else None
        ),
        "feedback_ids_sha256": hashlib.sha256(
            "\n".join(canonical[index]["feedback_id"] for index in sorted(canonical)).encode()
        ).hexdigest(),
    }
    return QualityApproval(
        tuple(decisions[index] for index in sorted(decisions)),
        evaluation.getvalue(),
        audit.getvalue(),
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )


def score_multilabel(
    gold: list[set[int]],
    predictions: list[set[int]],
) -> QualityMetrics:
    if len(gold) != len(predictions) or not gold:
        raise ValueError("Для оценки нужны пары эталонов и предсказаний")
    tp = sum(len(expected & actual) for expected, actual in zip(gold, predictions, strict=True))
    fp = sum(len(actual - expected) for expected, actual in zip(gold, predictions, strict=True))
    fn = sum(len(expected - actual) for expected, actual in zip(gold, predictions, strict=True))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    exact = sum(expected == actual for expected, actual in zip(gold, predictions, strict=True))
    return QualityMetrics(len(gold), precision, recall, f1, exact / len(gold), tp, fp, fn)


async def sample_csv(
    session: AsyncSession,
    *,
    count: int,
    exclude_ids: set[str] | None = None,
    unclassified_only: bool = False,
) -> tuple[str, str]:
    if count < 100:
        raise ValueError("Для проверки качества нужно не меньше 100 отзывов")
    reviews = await session.execute(
        text("""
            select f.id, f.valuation, f.created_date, p.account_key,
                   concat_ws(E'\n', nullif(f.text, ''), nullif(f.pros, ''),
                             nullif(f.cons, '')) as body
              from wb.feedbacks f
              join wb.products p on p.nm_id = f.nm_id
             where f.valuation <= 3
               and (nullif(f.text, '') is not null or nullif(f.pros, '') is not null
                    or nullif(f.cons, '') is not null)
               and not (f.id = any(cast(:exclude_ids as text[])))
               and (
                   not :unclassified_only
                   or not exists (
                       select 1 from wb.feedback_reasons fr where fr.feedback_id = f.id
                   ) and not exists (
                       select 1 from wb.ai_review_claims c where c.feedback_id = f.id
                   )
               )
             order by md5(f.id)
             limit :count
        """),
        {
            "count": count,
            "exclude_ids": sorted(exclude_ids or set()),
            "unclassified_only": unclassified_only,
        },
    )
    rows = reviews.all()
    if len(rows) < count:
        raise ValueError(f"Реальных текстовых негативных отзывов только {len(rows)}")
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "feedback_id",
            "account_key",
            "stars",
            "created_date",
            "text_clean",
            "gold_reason_ids",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.id,
                row.account_key,
                row.valuation,
                row.created_date.isoformat(),
                sanitize(str(row.body or "")),
                "",
            ]
        )
    reasons = await session.execute(
        text("select id, name from wb.negative_reasons where is_approved and is_active order by id")
    )
    catalog = StringIO()
    catalog_writer = csv.writer(catalog)
    catalog_writer.writerow(["reason_id", "reason_name"])
    for row in reasons.all():
        catalog_writer.writerow([row.id, row.name])
    return output.getvalue(), catalog.getvalue()


def _parse_reason_ids(raw: str, *, allowed: set[int]) -> set[int]:
    values = set()
    for item in raw.split(";"):
        item = item.strip()
        if not item:
            continue
        try:
            reason_id = int(item)
        except ValueError as exc:
            raise ValueError(f"Некорректный ID причины: {item}") from exc
        if reason_id not in allowed:
            raise ValueError(f"Неизвестный ID причины: {reason_id}")
        values.add(reason_id)
    if not values:
        raise ValueError("Для каждого отзыва нужна хотя бы одна эталонная причина")
    return values


async def evaluate_csv(session: AsyncSession, content: str) -> QualityMetrics:
    reader = csv.DictReader(StringIO(content.removeprefix("\ufeff")))
    required = {"feedback_id", "gold_reason_ids"}
    if reader.fieldnames is None or not required <= set(reader.fieldnames):
        raise ValueError("CSV должен содержать feedback_id и gold_reason_ids")
    entries = list(reader)
    if len(entries) < 100:
        raise ValueError("Для оценки нужно вручную разметить не меньше 100 отзывов")
    ids = [str(entry["feedback_id"] or "").strip() for entry in entries]
    if len(set(ids)) != len(ids) or not all(ids):
        raise ValueError("feedback_id должны быть заполнены и уникальны")
    reasons = await session.execute(
        text("select id from wb.negative_reasons where is_approved and is_active")
    )
    allowed = {int(row.id) for row in reasons.all()}
    gold = [
        _parse_reason_ids(str(entry["gold_reason_ids"] or ""), allowed=allowed) for entry in entries
    ]
    result = await session.execute(
        text("""
            select fr.feedback_id, fr.reason_id
              from wb.feedback_reasons fr
             where fr.feedback_id = any(cast(:ids as text[]))
        """),
        {"ids": ids},
    )
    predictions: dict[str, set[int]] = {feedback_id: set() for feedback_id in ids}
    for row in result.all():
        predictions[str(row.feedback_id)].add(int(row.reason_id))
    return score_multilabel(gold, [predictions[feedback_id] for feedback_id in ids])
