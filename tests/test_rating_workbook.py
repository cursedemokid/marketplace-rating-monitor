"""Excel-отчёт 1 остаётся редактируемым: числа, спарклайны и график."""

from __future__ import annotations

import datetime as dt
from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from app.repositories.reporting import RatingReport, RatingReportRow


def test_workbook_contains_two_skus_sparklines_and_fixed_chart_axis() -> None:
    from app.services.rating_workbook import make_rating_workbook

    report = RatingReport(
        start=dt.date(2026, 9, 10),
        end=dt.date(2026, 9, 11),
        dates=(dt.date(2026, 9, 10), dt.date(2026, 9, 11)),
        rows=(
            RatingReportRow(999_001_001, "REPORT-A", 4.9, 4.8, -0.1, 10, 5, 1, 3, 1),
            RatingReportRow(999_001_002, "REPORT-B", None, 4.6, None, None, None, None, None, None),
        ),
        ratings_by_day={999_001_001: (4.85, 4.8), 999_001_002: (None, 4.6)},
    )

    content = make_rating_workbook(report)

    with ZipFile(BytesIO(content)) as archive:
        names = set(archive.namelist())
        strings = archive.read("xl/sharedStrings.xml").decode()
        main = archive.read("xl/worksheets/sheet1.xml").decode()
        chart = archive.read("xl/charts/chart1.xml").decode()

    assert "xl/worksheets/sheet2.xml" in names
    assert "REPORT-A" in strings and "REPORT-B" in strings
    assert "Нет данных" in strings
    assert "sparkline" in main
    assert '<c:min val="4"' in chart or '<c:min val="4.0"' in chart
    assert '<c:max val="5"' in chart or '<c:max val="5.0"' in chart


def test_empty_selection_does_not_write_broken_average_formula() -> None:
    from app.services.rating_workbook import make_rating_workbook

    report = RatingReport(
        start=dt.date(2026, 9, 10),
        end=dt.date(2026, 9, 11),
        dates=(dt.date(2026, 9, 10), dt.date(2026, 9, 11)),
        rows=(),
        ratings_by_day={},
    )

    content = make_rating_workbook(report)

    with ZipFile(BytesIO(content)) as archive:
        sheet = archive.read("xl/worksheets/sheet2.xml").decode()
    assert "B3:B2" not in sheet
    assert "C3:C2" not in sheet


def test_rating_dates_are_numeric_with_excel_invariant_literal_dots() -> None:
    from app.services.rating_workbook import make_rating_workbook

    report = RatingReport(
        start=dt.date(2026, 10, 7),
        end=dt.date(2026, 10, 8),
        dates=(dt.date(2026, 10, 7), dt.date(2026, 10, 8)),
        rows=(RatingReportRow(999_001_001, "REPORT-A", 4.9, 4.8, -0.1, 10, 5, 1, 3, 1),),
        ratings_by_day={999_001_001: (4.9, 4.8)},
    )

    content = make_rating_workbook(report)
    ns = {"x": "http://service.example.invalid/spreadsheetml/2006/main"}
    chart_ns = {"c": "http://service.example.invalid/drawingml/2006/chart"}
    with ZipFile(BytesIO(content)) as archive:
        styles = ET.fromstring(archive.read("xl/styles.xml"))
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet2.xml"))
        chart = ET.fromstring(archive.read("xl/charts/chart1.xml"))

    formats = {
        item.attrib["numFmtId"]: item.attrib["formatCode"]
        for item in styles.findall("x:numFmts/x:numFmt", ns)
    }
    date_styles = {
        index: formats.get(xf.attrib.get("numFmtId", ""))
        for index, xf in enumerate(styles.findall("x:cellXfs/x:xf", ns))
    }
    cells = {cell.attrib["r"]: cell for cell in sheet.findall(".//x:c", ns)}
    assert cells["B1"].findtext("x:v", namespaces=ns) == "46302"
    assert cells["C1"].findtext("x:v", namespaces=ns) == "46303"
    assert date_styles[int(cells["B1"].attrib["s"])] == 'dd"."mm"."yyyy'
    assert date_styles[int(cells["C1"].attrib["s"])] == 'dd"."mm"."yyyy'
    axis_format = chart.find(".//c:dateAx/c:numFmt", chart_ns)
    assert axis_format is not None
    assert axis_format.attrib["formatCode"] == 'dd"."mm'


def test_comparison_columns_are_numeric_and_daily_charts_use_new_counts() -> None:
    """Catches exports that silently keep only the legacy absolute deltas."""
    from app.services.comparison import Change, Comparison, DailyPoint, PeriodMetrics
    from app.services.rating_workbook import make_rating_workbook

    counts = {"total": 10, "5": 7, "4": 1, "3": 0, "2": 0, "1": 2, "negative": 2}
    shares = {"5": 0.7, "4": 0.1, "3": 0.0, "2": 0.0, "1": 0.2, "negative": 0.2}
    changes = {key: Change(100.0, "ok") for key in counts}
    share_changes = {key: Change(0.0, "ok") for key in shares}
    comparison = Comparison(
        current=PeriodMetrics({key: value * 2 for key, value in counts.items()}, shares, "ok"),
        previous=PeriodMetrics(counts, shares, "ok"),
        rating_change_pct=Change(-0.208333333333333, "ok"),
        count_changes_pct=changes,
        share_changes_pct=share_changes,
        share_changes_pp=share_changes,
        buyout_previous=89.2,
        buyout_current=89.0,
        buyout_delta_pp=Change(-0.2, "ok"),
    )
    report = RatingReport(
        start=dt.date(2026, 10, 18),
        end=dt.date(2026, 10, 19),
        previous_start=dt.date(2026, 10, 16),
        previous_end=dt.date(2026, 10, 17),
        dates=(dt.date(2026, 10, 18), dt.date(2026, 10, 19)),
        rows=(
            RatingReportRow(
                999001001,
                "REPORT-A",
                4.8,
                4.79,
                -0.01,
                7,
                1,
                0,
                0,
                2,
                title="Тестовый товар",
                comparison=comparison,
            ),
        ),
        ratings_by_day={999001001: (4.8, 4.79)},
        daily_by_product={
            999001001: (
                DailyPoint(dt.date(2026, 10, 18), counts, shares, "ok"),
                DailyPoint(
                    dt.date(2026, 10, 19), dict.fromkeys(counts), dict.fromkeys(shares), "missing"
                ),
            )
        },
    )
    ns = {"x": "http://service.example.invalid/spreadsheetml/2006/main"}
    with ZipFile(BytesIO(make_rating_workbook(report))) as archive:
        strings = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        labels = ["".join(item.itertext()) for item in strings]
        main = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        rows = main.findall(".//x:row", ns)
        header = next(row for row in rows if row.attrib["r"] == "6")
        columns = {
            labels[int(cell.findtext("x:v", namespaces=ns))]: cell.attrib["r"].rstrip("3228787319")
            for cell in header.findall("x:c", ns)
            if cell.get("t") == "s"
        }
        assert "Изменение рейтинга, %" in columns
        assert "5★: изменение количества, %" in columns
        assert "5★: изменение доли, п.п." in columns
        assert "Изменение выкупа, п.п." in columns
        data_row = next(row for row in rows if row.attrib["r"] == "7")
        data = {cell.attrib["r"]: cell for cell in data_row.findall("x:c", ns)}
        key = columns["5★: изменение количества, %"] + "7"
        assert float(data[key].findtext("x:v", namespaces=ns)) == 1.0
        key = columns["Изменение выкупа, п.п."] + "7"
        assert float(data[key].findtext("x:v", namespaces=ns)) == -0.2
        assert "Тестовый товар" in labels
        assert len([name for name in archive.namelist() if name.startswith("xl/charts/chart")]) >= 5
        daily = ET.fromstring(archive.read("xl/worksheets/sheet3.xml"))
        daily_cells = {cell.attrib["r"]: cell for cell in daily.findall(".//x:c", ns)}
        assert daily_cells["B2"].findtext("x:v", namespaces=ns) == "10"
        assert daily_cells["C2"].findtext("x:v", namespaces=ns) == "2"
        assert daily_cells["D2"].findtext("x:v", namespaces=ns) == "0.2"
        assert daily_cells["B3"].findtext("x:v", namespaces=ns) in {None, ""}
        chart = ET.fromstring(archive.read("xl/charts/chart2.xml"))
        chart_ns = {"c": "http://service.example.invalid/drawingml/2006/chart"}
        blanks = chart.find("c:dispBlanksAs", chart_ns)
        assert blanks is None or blanks.attrib["val"] == "gap"


def test_daily_product_code_is_text_even_if_it_starts_with_equals() -> None:
    from app.services.comparison import DailyPoint
    from app.services.rating_workbook import make_rating_workbook

    day = dt.date(2026, 10, 19)
    counts = {"total": 1, "5": 1, "4": 0, "3": 0, "2": 0, "1": 0, "negative": 0}
    report = RatingReport(
        start=day,
        end=day,
        dates=(day,),
        rows=(RatingReportRow(999001001, "=1+1", 4.9, 4.9, 0, 1, 0, 0, 0, 0),),
        ratings_by_day={999001001: (4.9,)},
        daily_by_product={999001001: (DailyPoint(day, counts, {}, "ok"),)},
    )
    ns = {"x": "http://service.example.invalid/spreadsheetml/2006/main"}
    with ZipFile(BytesIO(make_rating_workbook(report))) as archive:
        daily = ET.fromstring(archive.read("xl/worksheets/sheet3.xml"))
        source_row = daily.findall(".//x:row", ns)[-1]
        code = next(
            cell for cell in source_row.findall("x:c", ns) if cell.attrib["r"].startswith("C")
        )
        assert code.attrib.get("t") == "s"
        assert code.find("x:f", ns) is None
