"""Excel-отчёт причин содержит процентную матрицу и живой график итога."""

from __future__ import annotations

import datetime as dt
from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from app.repositories.reporting import ReasonReport, ReasonReportRow

NS = {"s": "http://service.example.invalid/spreadsheetml/2006/main"}


def test_reference_reasons_are_visible_but_not_company_complaints_in_charts() -> None:
    from app.services.reason_workbook import make_reason_workbook

    day = dt.date(2026, 10, 18)
    report = ReasonReport(
        start=day,
        end=day,
        dates=(day,),
        rows=(
            ReasonReportRow(1, "протекают", (5,), (0.5,)),
            ReasonReportRow(2, "причина не указана", (2,), (0.2,)),
            ReasonReportRow(3, "работа ПВЗ", (6,), (0.6,), is_external=True),
            ReasonReportRow(4, "пустой отзыв", (1,), (0.1,)),
        ),
        negative_counts=(10,),
        identified_counts=(6,),
        total_shares=(0.6,),
    )
    with ZipFile(BytesIO(make_reason_workbook(report))) as archive:
        main = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        counts = ET.fromstring(archive.read("xl/worksheets/sheet2.xml"))
        strings = archive.read("xl/sharedStrings.xml").decode()
        charts = [
            archive.read(name).decode()
            for name in archive.namelist()
            if name.startswith("xl/charts/chart") and name.endswith(".xml")
        ]
    assert "причина не указана" in strings and "работа ПВЗ" in strings
    for address, share, count in (("B8", 0.2, 2), ("B9", 0.6, 6)):
        assert float(main.find(f".//s:c[@r='{address}']/s:v", NS).text) == share
        assert int(counts.find(f".//s:c[@r='{address}']/s:v", NS).text) == count
    assert float(main.find(".//s:c[@r='B11']/s:v", NS).text) == 0.6
    # Reference rows remain visible without severity colours or product-cause rankings.
    severity_ranges = {node.attrib["sqref"] for node in main.findall("s:conditionalFormatting", NS)}
    assert severity_ranges == {"B7", "B10"}
    assert len(charts) == 5
    assert not any("работа ПВЗ" in chart or "причина не указана" in chart for chart in charts)


def test_reason_heatmap_has_absolute_green_zero_and_red_hundred_bounds() -> None:
    from app.services.reason_workbook import make_reason_workbook

    report = ReasonReport(
        start=dt.date(2026, 10, 7),
        end=dt.date(2026, 10, 8),
        dates=(dt.date(2026, 10, 7), dt.date(2026, 10, 8)),
        rows=(ReasonReportRow(1, "протекают", (2, 3), (0.2, 0.3)),),
        negative_counts=(10, 10),
        identified_counts=(2, 3),
        total_shares=(0.2, 0.3),
    )
    with ZipFile(BytesIO(make_reason_workbook(report))) as archive:
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    scale = sheet.find(".//s:colorScale", NS)
    assert scale is not None
    assert [p.attrib for p in scale.findall("s:cfvo", NS)] == [
        {"type": "num", "val": "0"},
        {"type": "num", "val": "0.5"},
        {"type": "num", "val": "1"},
    ]
    assert [p.attrib["rgb"] for p in scale.findall("s:color", NS)] == [
        "FFC6EFCE",
        "FFFFEB9C",
        "FFFFC7CE",
    ]
    assert sheet.find("s:conditionalFormatting", NS).attrib["sqref"] == "B7:C7"


def test_reason_charts_use_weighted_period_shares_and_distinct_daily_counts() -> None:
    from app.services.reason_workbook import make_reason_workbook

    report = ReasonReport(
        start=dt.date(2026, 10, 7),
        end=dt.date(2026, 10, 8),
        dates=(dt.date(2026, 10, 7), dt.date(2026, 10, 8)),
        rows=(
            ReasonReportRow(1, "протекают", (5, 9), (0.5, 0.1)),
            ReasonReportRow(2, "впитываемость", (4, 8), (0.4, 8 / 90)),
        ),
        negative_counts=(10, 90),
        identified_counts=(6, 10),
        total_shares=(0.6, 10 / 90),
    )
    with ZipFile(BytesIO(make_reason_workbook(report))) as archive:
        charts = [
            archive.read(name).decode()
            for name in archive.namelist()
            if name.startswith("xl/charts/chart") and name.endswith(".xml")
        ]
        workbook = archive.read("xl/workbook.xml").decode()
        assert "Графики" in workbook
        dashboard = ET.fromstring(archive.read("xl/worksheets/sheet3.xml"))
    assert "Графики" in workbook
    assert len(charts) == 5
    assert any('barDir val="bar"' in chart for chart in charts)
    assert any('grouping val="stacked"' in chart for chart in charts)
    assert any("Причины!$B$7:$C$7" in chart for chart in charts)  # доля по дням
    assert any("Объёмы!$B$7:$C$7" in chart for chart in charts)  # количество по дням
    # 14 / 100 = 14%, а не среднее дневных долей (50% + 10%) / 2 = 30%.
    share = dashboard.find("s:sheetData/s:row/s:c[@r='F50']", NS)
    assert share is not None
    assert float(share.find("s:v", NS).text) == 0.14
    assert share.find("s:f", NS) is not None


def test_reason_dates_keep_numeric_values_with_literal_dots() -> None:
    from app.services.reason_workbook import make_reason_workbook

    day = dt.date(2026, 10, 7)
    report = ReasonReport(
        start=day,
        end=day,
        dates=(day,),
        rows=(),
        negative_counts=(0,),
        identified_counts=(0,),
        total_shares=(None,),
    )
    with ZipFile(BytesIO(make_reason_workbook(report))) as archive:
        styles = ET.fromstring(archive.read("xl/styles.xml"))
        formats = {
            f.attrib["numFmtId"]: f.attrib["formatCode"]
            for f in styles.findall("s:numFmts/s:numFmt", NS)
        }
        xfs = styles.findall("s:cellXfs/s:xf", NS)
        for name in ("sheet1", "sheet2"):
            sheet = ET.fromstring(archive.read(f"xl/worksheets/{name}.xml"))
            cell = sheet.find("s:sheetData/s:row/s:c[@r='B6']", NS)
            assert cell is not None and cell.attrib.get("t", "n") == "n"
            assert cell.find("s:v", NS).text == "46302"
            assert formats[xfs[int(cell.attrib["s"])].attrib["numFmtId"]] == 'dd"."mm"."yyyy'


def test_reason_workbook_uses_numeric_shares_and_unique_total_chart() -> None:
    from app.services.reason_workbook import make_reason_workbook

    report = ReasonReport(
        start=dt.date(2026, 9, 10),
        end=dt.date(2026, 9, 12),
        dates=(dt.date(2026, 9, 10), dt.date(2026, 9, 11), dt.date(2026, 9, 12)),
        rows=(
            ReasonReportRow(1, "протекают", (2, 0, 0), (2 / 3, 0.0, None)),
            ReasonReportRow(2, "маломерят", (2, 0, 0), (2 / 3, 0.0, None)),
        ),
        negative_counts=(3, 1, 0),
        identified_counts=(3, 0, 0),
        total_shares=(1.0, 0.0, None),
    )

    content = make_reason_workbook(report)

    with ZipFile(BytesIO(content)) as archive:
        names = set(archive.namelist())
        strings = archive.read("xl/sharedStrings.xml").decode()
        main = archive.read("xl/worksheets/sheet1.xml").decode()
        counts = archive.read("xl/worksheets/sheet2.xml").decode()
        chart = archive.read("xl/charts/chart1.xml").decode()

    assert "xl/worksheets/sheet2.xml" in names
    assert all(label in strings for label in ("протекают", "маломерят", "Общий итог"))
    assert "Каждая доля" in strings and "уникальных" in strings
    assert "conditionalFormatting" in main and 'type="colorScale"' in main
    assert "0.666666" in main
    assert "$B$9:$D$9" in chart  # график берёт строку уникального итога
    assert "3" in counts  # знаменатель за первый день


def test_reason_workbook_accepts_empty_selection() -> None:
    from app.services.reason_workbook import make_reason_workbook

    report = ReasonReport(
        start=dt.date(2026, 9, 10),
        end=dt.date(2026, 9, 10),
        dates=(dt.date(2026, 9, 10),),
        rows=(),
        negative_counts=(0,),
        identified_counts=(0,),
        total_shares=(None,),
    )
    with ZipFile(BytesIO(make_reason_workbook(report))) as archive:
        main = archive.read("xl/worksheets/sheet1.xml").decode()
    assert "conditionalFormatting" not in main
