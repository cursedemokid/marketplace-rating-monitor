from __future__ import annotations

from io import BytesIO

import xlsxwriter
from app.repositories.nomenclature import ProductIdentity
from app.services.nomenclature import NomenclatureService, parse_nomenclature_xlsx


def _workbook(rows: list[list[object]]) -> bytes:
    output = BytesIO()
    book = xlsxwriter.Workbook(output, {"in_memory": True})
    sheet = book.add_worksheet("Номенклатура")
    headers = [
        "Кабинет",
        "Код WB",
        "Артикул продавца",
        "Название WB",
        "Бренд WB",
        "Предмет WB",
        "Размер WB",
        "Пачка",
        "Линейка",
        "Тип",
        "Вложимость",
        "Категория",
        "Бренд-2",
        "Комплект",
        "Количество в комплекте",
    ]
    sheet.write_row(5, 0, headers)
    for index, row in enumerate(rows, start=6):
        sheet.write_row(index, 0, row)
    book.close()
    return output.getvalue()


def test_parser_preserves_empty_fields_as_null_and_maps_account_labels() -> None:
    raw = _workbook(
        [
            [
                "НК Джакс",
                2485901835,
                "DemoPhone",
                "Товар",
                "Бренд",
                "Предмет",
                "0",
                "",
                "",
                "Тип",
                "1500 мл",
                "Категория",
                "",
                "",
                "",
            ],
            [
                "Элалис",
                3890373038,
                "DemoPhone",
                "Другой товар",
                "Марка",
                "Предмет",
                "1",
                "Короб",
                "Линия",
                "Тип",
                "500 мл",
                "Категория",
                "Марка 2",
                "Набор",
                2,
            ],
        ]
    )

    parsed = parse_nomenclature_xlsx(raw)

    assert len(parsed.rows) == 2
    assert parsed.rows[0].account_key == "default"
    assert parsed.rows[0].vendor_code == "DemoPhone"
    assert parsed.rows[0].pack is None
    assert parsed.rows[0].brand2 is None
    assert parsed.rows[0].kit_qty is None
    assert parsed.rows[1].account_key == "DemoSeller"
    assert parsed.rows[1].brand2 == "Марка 2"
    assert parsed.rows[1].kit_qty == 2
    assert parsed.row_errors == []


def test_parser_reports_invalid_account_and_non_integer_kit_quantity() -> None:
    raw = _workbook(
        [
            [
                "Неизвестный кабинет",
                1,
                "seller-1",
                "Товар",
                "Бренд",
                "Предмет",
                "0",
                "",
                "",
                "Тип",
                "500 мл",
                "Категория",
                "",
                "",
                "много",
            ]
        ]
    )

    parsed = parse_nomenclature_xlsx(raw)

    assert parsed.rows == []
    assert [error.row_number for error in parsed.row_errors] == [7]
    assert "Кабинет" in parsed.row_errors[0].message
    assert "Количество в комплекте" in parsed.row_errors[0].message


async def test_preview_separates_duplicate_unknown_and_discrepant_products() -> None:
    raw = _workbook(
        [
            [
                "НК Джакс",
                10,
                "known",
                "Товар",
                "Бренд",
                "Предмет",
                "0",
                "",
                "",
                "Тип",
                "500 мл",
                "Категория",
                "",
                "",
                "",
            ],
            [
                "НК Джакс",
                11,
                "known",
                "Товар",
                "Бренд",
                "Предмет",
                "0",
                "",
                "",
                "Тип",
                "500 мл",
                "Категория",
                "",
                "",
                "",
            ],
            [
                "НК Джакс",
                20,
                "missing",
                "Товар",
                "Бренд",
                "Предмет",
                "0",
                "",
                "",
                "Тип",
                "500 мл",
                "Категория",
                "",
                "",
                "",
            ],
            [
                "Элалис",
                30,
                "known",
                "Товар",
                "Бренд",
                "Предмет",
                "0",
                "",
                "",
                "Тип",
                "500 мл",
                "Категория",
                "",
                "",
                "",
            ],
        ]
    )

    class Result:
        def all(self) -> list[ProductIdentity]:
            return [
                ProductIdentity("default", "known", 10),
                ProductIdentity("DemoSeller", "known", 31),
                ProductIdentity("default", "omitted", 99),
            ]

    class Session:
        async def execute(self, _query: object) -> Result:
            return Result()

    preview = await NomenclatureService(Session()).preview_import_xlsx(raw)  # type: ignore[arg-type]

    assert [issue.row_number for issue in preview.duplicates] == [7, 8]
    assert [issue.row_number for issue in preview.unknown_products] == [9]
    assert [issue.row_number for issue in preview.discrepancies] == [8, 10]
    assert [
        (issue.account_key, issue.vendor_code, issue.nm_id) for issue in preview.missing_in_file
    ] == [("default", "omitted", 99)]
    assert preview.valid_count == 0
    assert not preview.can_import


async def test_products_missing_from_workbook_do_not_block_import() -> None:
    raw = _workbook(
        [
            [
                "НК Джакс",
                10,
                "present",
                "Товар",
                "Бренд",
                "Предмет",
                "0",
                "",
                "",
                "Тип",
                "500 мл",
                "Категория",
                "",
                "",
                "",
            ]
        ]
    )

    class Result:
        def all(self) -> list[ProductIdentity]:
            return [
                ProductIdentity("default", "present", 10),
                ProductIdentity("default", "not-in-file", 20),
            ]

    class Session:
        async def execute(self, _query: object) -> Result:
            return Result()

    preview = await NomenclatureService(Session()).preview_import_xlsx(raw)  # type: ignore[arg-type]

    assert preview.can_import
    assert preview.valid_count == 1
    assert [(issue.vendor_code, issue.nm_id) for issue in preview.missing_in_file] == [
        ("not-in-file", 20)
    ]
