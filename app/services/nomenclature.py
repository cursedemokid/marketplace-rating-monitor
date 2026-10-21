"""Импорт и валидация номенклатуры из XLSX."""

from __future__ import annotations

import re
import zipfile
from collections import Counter
from dataclasses import dataclass
from io import BytesIO
from typing import cast
from xml.etree import ElementTree

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.nomenclature import (
    NomenclatureInput,
    NomenclatureRecord,
    NomenclatureRepository,
)

_NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL_NS = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
_ACCOUNT_KEYS = {"НК Джакс": "default", "Элалис": "DemoSeller"}
_HEADERS = {
    "Кабинет": "account",
    "Код WB": "nm_id",
    "Артикул продавца": "vendor_code",
    "Пачка": "pack",
    "Линейка": "line",
    "Тип": "type",
    "Вложимость": "capacity",
    "Категория": "category",
    "Бренд-2": "brand2",
    "Комплект": "kit",
    "Количество в комплекте": "kit_qty",
}
_STRING_FIELDS = ("pack", "line", "type", "capacity", "category", "brand2", "kit")
_MAX_XLSX_BYTES = 10 * 1024 * 1024
_MAX_UNPACKED_BYTES = 30 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class WorkbookRow(NomenclatureInput):
    row_number: int = 0
    nm_id: int = 0


@dataclass(frozen=True, slots=True)
class RowError:
    row_number: int
    message: str


@dataclass(frozen=True, slots=True)
class RowIssue:
    row_number: int | None
    account_key: str
    vendor_code: str
    message: str
    nm_id: int | None = None


@dataclass(frozen=True, slots=True)
class ParsedNomenclature:
    rows: list[WorkbookRow]
    row_errors: list[RowError]


@dataclass(frozen=True, slots=True)
class ImportPreview:
    rows: list[WorkbookRow]
    row_errors: list[RowError]
    duplicates: list[RowIssue]
    unknown_products: list[RowIssue]
    discrepancies: list[RowIssue]
    missing_in_file: list[RowIssue]

    @property
    def valid_count(self) -> int:
        rejected = {
            issue.row_number
            for issue in (*self.duplicates, *self.unknown_products, *self.discrepancies)
        }
        return sum(row.row_number not in rejected for row in self.rows)

    @property
    def can_import(self) -> bool:
        return not (
            self.row_errors or self.duplicates or self.unknown_products or self.discrepancies
        )


def _column_number(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference)
    if letters is None:
        raise ValueError("Некорректная ссылка на ячейку")
    value = 0
    for letter in letters.group():
        value = value * 26 + ord(letter) - ord("A") + 1
    return value - 1


def _cell_value(cell: ElementTree.Element, shared_strings: list[str]) -> str | None:
    kind = cell.attrib.get("t")
    value: str | None
    if kind == "inlineStr":
        value = "".join(cell.itertext())
    else:
        node = cell.find("x:v", _NS)
        value = node.text if node is not None else None
        if kind == "s" and value is not None:
            value = shared_strings[int(value)]
    if value is None or not value.strip():
        return None
    return value.strip()


def _worksheet_path(archive: zipfile.ZipFile) -> str:
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    sheet = workbook.find("x:sheets/x:sheet", _NS)
    if sheet is None:
        raise ValueError("В XLSX нет листов")
    rel_id = sheet.attrib.get(_REL_NS + "id")
    if rel_id is None:
        raise ValueError("Не удалось определить лист XLSX")
    rels = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    for relation in rels.findall("r:Relationship", _PKG_REL_NS):
        if relation.attrib.get("Id") == rel_id:
            target = relation.attrib["Target"].lstrip("/")
            return target if target.startswith("xl/") else f"xl/{target}"
    raise ValueError("Не удалось прочитать первый лист XLSX")


def parse_nomenclature_xlsx(content: bytes) -> ParsedNomenclature:
    """Распарсить XLSX, не заполняя и не угадывая пустые значения."""
    if not content or len(content) > _MAX_XLSX_BYTES:
        raise ValueError("Размер XLSX должен быть от 1 байта до 10 МБ")
    try:
        archive = zipfile.ZipFile(BytesIO(content))
        if sum(item.file_size for item in archive.infolist()) > _MAX_UNPACKED_BYTES:
            raise ValueError("Распакованный XLSX превышает 30 МБ")
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            strings = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared_strings = ["".join(item.itertext()) for item in strings.findall("x:si", _NS)]
        sheet = ElementTree.fromstring(archive.read(_worksheet_path(archive)))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, IndexError) as exc:
        raise ValueError("Файл не является корректным XLSX") from exc

    raw_rows: list[tuple[int, dict[int, str | None]]] = []
    for row in sheet.findall(".//x:sheetData/x:row", _NS):
        values = {
            _column_number(cell.attrib["r"]): _cell_value(cell, shared_strings)
            for cell in row.findall("x:c", _NS)
        }
        raw_rows.append((int(row.attrib.get("r", "0")), values))
    header_index = next(
        (
            index
            for index, (_, values) in enumerate(raw_rows)
            if any(value == "Артикул продавца" for value in values.values())
        ),
        None,
    )
    if header_index is None:
        raise ValueError("В XLSX не найдена колонка «Артикул продавца»")
    header_values = raw_rows[header_index][1]
    positions = {
        name: index
        for index, value in header_values.items()
        if value in _HEADERS
        for name in (_HEADERS[value],)
    }
    missing = set(_HEADERS.values()) - set(positions)
    if missing:
        raise ValueError("В XLSX отсутствуют обязательные колонки")

    rows: list[WorkbookRow] = []
    errors: list[RowError] = []
    for row_number, values in raw_rows[header_index + 1 :]:
        if not any(value is not None for value in values.values()):
            continue
        errors_for_row: list[str] = []
        account_label = values.get(positions["account"])
        account_key = _ACCOUNT_KEYS.get(account_label or "")
        if account_key is None:
            errors_for_row.append("Неизвестное значение в колонке «Кабинет»")
        vendor_code = values.get(positions["vendor_code"])
        if not vendor_code:
            errors_for_row.append("Не заполнен «Артикул продавца»")
        nm_text = values.get(positions["nm_id"])
        try:
            nm_id = int(nm_text or "")
            if nm_id <= 0:
                raise ValueError
        except ValueError:
            nm_id = 0
            errors_for_row.append("«Код WB» должен быть положительным целым числом")
        quantity_text = values.get(positions["kit_qty"])
        kit_qty: int | None = None
        if quantity_text is not None:
            try:
                if not re.fullmatch(r"\d+", quantity_text):
                    raise ValueError
                kit_qty = int(quantity_text)
            except ValueError:
                errors_for_row.append("«Количество в комплекте» должно быть целым числом")
        field_values = {field: values.get(positions[field]) for field in _STRING_FIELDS}
        if any(value is not None and len(value) > 500 for value in field_values.values()):
            errors_for_row.append("Значение текстового поля превышает 500 символов")
        if errors_for_row:
            errors.append(RowError(row_number, "; ".join(errors_for_row)))
            continue
        rows.append(
            WorkbookRow(
                account_key=account_key or "",
                vendor_code=vendor_code or "",
                nm_id=nm_id,
                row_number=row_number,
                kit_qty=kit_qty,
                **field_values,
            )
        )
    return ParsedNomenclature(rows, errors)


class NomenclatureService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = NomenclatureRepository(session)

    async def preview_import_xlsx(self, content: bytes) -> ImportPreview:
        parsed = parse_nomenclature_xlsx(content)
        counts = Counter((row.account_key, row.vendor_code) for row in parsed.rows)
        duplicates = [
            RowIssue(
                row.row_number,
                row.account_key,
                row.vendor_code,
                "Повторяется пара кабинет + артикул продавца",
                row.nm_id,
            )
            for row in parsed.rows
            if counts[(row.account_key, row.vendor_code)] > 1
        ]
        products = await self._repo.products()
        by_key = {(product.account_key, product.vendor_code): product for product in products}
        by_nm = {(product.account_key, product.nm_id): product for product in products}
        workbook_keys = {(row.account_key, row.vendor_code) for row in parsed.rows}
        unknown: list[RowIssue] = []
        discrepancies: list[RowIssue] = []
        missing_in_file = [
            RowIssue(
                None,
                product.account_key,
                product.vendor_code,
                "Товар из базы отсутствует в файле",
                product.nm_id,
            )
            for product in products
            if (product.account_key, product.vendor_code) not in workbook_keys
        ]
        for row in parsed.rows:
            product = by_key.get((row.account_key, row.vendor_code))
            if product is None:
                other = by_nm.get((row.account_key, row.nm_id))
                if other is None:
                    unknown.append(
                        RowIssue(
                            row.row_number,
                            row.account_key,
                            row.vendor_code,
                            "Товар не найден в кабинете",
                            row.nm_id,
                        )
                    )
                else:
                    discrepancies.append(
                        RowIssue(
                            row.row_number,
                            row.account_key,
                            row.vendor_code,
                            f"Код WB соответствует артикулу {other.vendor_code}",
                            row.nm_id,
                        )
                    )
            elif product.nm_id != row.nm_id:
                discrepancies.append(
                    RowIssue(
                        row.row_number,
                        row.account_key,
                        row.vendor_code,
                        f"Для артикула в базе указан код WB {product.nm_id}",
                        row.nm_id,
                    )
                )
        return ImportPreview(
            parsed.rows, parsed.row_errors, duplicates, unknown, discrepancies, missing_in_file
        )

    async def confirm_import_xlsx(self, content: bytes) -> int:
        """Повторно валидировать исходный файл и выполнить атомарный upsert."""
        preview = await self.preview_import_xlsx(content)
        if not preview.can_import:
            raise ValueError("Импорт заблокирован: исправьте ошибки предпросмотра")
        async with self._session.begin_nested():
            return await self._repo.upsert_many(preview.rows)

    async def get_product(self, account_key: str, vendor_code: str) -> NomenclatureRecord | None:
        if account_key not in _ACCOUNT_KEYS.values() or not vendor_code.strip():
            return None
        vendor_code = vendor_code.strip()
        existing = await self._repo.get(account_key, vendor_code)
        if existing is not None:
            return existing
        products = await self._repo.products()
        if not any(
            product.account_key == account_key and product.vendor_code == vendor_code
            for product in products
        ):
            return None
        return NomenclatureRecord(account_key=account_key, vendor_code=vendor_code)

    async def update_product(
        self, account_key: str, vendor_code: str, fields: dict[str, object]
    ) -> NomenclatureRecord | None:
        allowed = set(_STRING_FIELDS) | {"kit_qty"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError("Переданы неизвестные поля номенклатуры")
        values: dict[str, str | int | None] = {}
        for field in _STRING_FIELDS:
            if field not in fields:
                continue
            value = fields[field]
            if value is not None and not isinstance(value, str):
                raise ValueError(f"Поле «{field}» должно быть строкой или null")
            normalized = value.strip() if isinstance(value, str) else None
            if normalized == "":
                normalized = None
            if normalized is not None and len(normalized) > 500:
                raise ValueError(f"Поле «{field}» превышает 500 символов")
            values[field] = normalized
        quantity = fields.get("kit_qty")
        if quantity is not None and (
            isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0
        ):
            raise ValueError("Поле «kit_qty» должно быть неотрицательным целым числом или null")
        if "kit_qty" in fields:
            values["kit_qty"] = quantity if isinstance(quantity, int) else None
        if account_key not in {"default", "DemoSeller"}:
            raise ValueError("Неизвестный кабинет")
        if not vendor_code.strip():
            raise ValueError("Артикул продавца обязателен")
        vendor_code = vendor_code.strip()
        products = await self._repo.products()
        if not any(
            product.account_key == account_key and product.vendor_code == vendor_code
            for product in products
        ):
            return None
        existing = await self._repo.get(account_key, vendor_code)
        if existing is None:
            default_values: dict[str, str | int | None] = {
                field: values.get(field) for field in (*_STRING_FIELDS, "kit_qty")
            }
            await self._repo.upsert_many(
                [
                    NomenclatureInput(
                        account_key=account_key,
                        vendor_code=vendor_code,
                        pack=cast(str | None, default_values["pack"]),
                        line=cast(str | None, default_values["line"]),
                        type=cast(str | None, default_values["type"]),
                        capacity=cast(str | None, default_values["capacity"]),
                        category=cast(str | None, default_values["category"]),
                        brand2=cast(str | None, default_values["brand2"]),
                        kit=cast(str | None, default_values["kit"]),
                        kit_qty=cast(int | None, default_values["kit_qty"]),
                    )
                ]
            )
            return await self._repo.get(account_key, vendor_code)
        return await self._repo.update_fields(account_key, vendor_code, values)
