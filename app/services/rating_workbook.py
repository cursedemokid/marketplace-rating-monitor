"""Excel-отчёт 1: рейтинг и приросты оценок за выбранный период."""

from __future__ import annotations

import datetime as dt
from io import BytesIO

import xlsxwriter
from xlsxwriter.format import Format
from xlsxwriter.utility import xl_col_to_name

from app.repositories.reporting import RatingReport
from app.services.comparison import Change, Comparison

STATUS_LABELS = {
    "ok": "Полные",
    "new": "новое",
    "zero": "—",
    "missing": "Нет данных",
    "empty": "Нет оценок",
    "correction": "Корректировка WB",
}


def _change_cell(item: Change | None, *, percent: bool = True) -> float | str:
    if item is None:
        return STATUS_LABELS["missing"]
    if item.value is None:
        return STATUS_LABELS.get(item.status, item.status)
    return item.value / 100 if percent else item.value


def _comparison_cells(comp: Comparison | None) -> list[tuple[str, float | str | None, str]]:
    cells: list[tuple[str, float | str | None, str]] = [
        (
            "Изменение рейтинга, %",
            _change_cell(comp.rating_change_pct if comp else None),
            "percent",
        ),
    ]
    for key, label in (
        ("total", "Всего"),
        ("5", "5★"),
        ("4", "4★"),
        ("3", "3★"),
        ("2", "2★"),
        ("1", "1★"),
        ("negative", "Негатив"),
    ):
        cells.extend(
            [
                (
                    f"{label}: предыдущий период",
                    comp.previous.counts[key] if comp else None,
                    "count",
                ),
                (f"{label}: текущий период", comp.current.counts[key] if comp else None, "count"),
                (
                    f"{label}: изменение количества, %",
                    _change_cell(comp.count_changes_pct[key] if comp else None),
                    "percent",
                ),
            ]
        )
        if key != "total":
            cells.extend(
                [
                    (
                        f"{label}: доля раньше, %",
                        comp.previous.shares[key] if comp else None,
                        "share",
                    ),
                    (
                        f"{label}: доля сейчас, %",
                        comp.current.shares[key] if comp else None,
                        "share",
                    ),
                    (
                        f"{label}: изменение доли, %",
                        _change_cell(comp.share_changes_pct[key] if comp else None),
                        "percent",
                    ),
                    (
                        f"{label}: изменение доли, п.п.",
                        _change_cell(comp.share_changes_pp[key] if comp else None, percent=False),
                        "pp",
                    ),
                ]
            )
    cells.extend(
        [
            (
                "Выкуп: предыдущий период, %",
                comp.buyout_previous / 100 if comp and comp.buyout_previous is not None else None,
                "share",
            ),
            (
                "Выкуп: текущий период, %",
                comp.buyout_current / 100 if comp and comp.buyout_current is not None else None,
                "share",
            ),
            (
                "Изменение выкупа, п.п.",
                _change_cell(comp.buyout_delta_pp if comp else None, percent=False),
                "pp",
            ),
            (
                "Данные предыдущего периода",
                STATUS_LABELS[comp.previous.status] if comp else "Нет данных",
                "text",
            ),
            (
                "Данные текущего периода",
                STATUS_LABELS[comp.current.status] if comp else "Нет данных",
                "text",
            ),
        ]
    )
    return cells


def make_rating_workbook(report: RatingReport) -> bytes:
    """Создать редактируемую книгу; отсутствие границы остаётся пустой ячейкой."""
    output = BytesIO()
    book = xlsxwriter.Workbook(output, {"in_memory": True})
    main = book.add_worksheet("Динамика")
    series = book.add_worksheet("Ряды")

    title = book.add_format({"bold": True, "font_size": 17, "font_color": "#183153"})
    note = book.add_format({"font_color": "#506175", "text_wrap": True})
    header = book.add_format(
        {"bold": True, "font_color": "#FFFFFF", "bg_color": "#183153", "text_wrap": True}
    )
    rating_fmt = book.add_format({"num_format": "0.00"})
    delta_fmt = book.add_format({"num_format": "+0.00;-0.00;0.00"})
    count_fmt = book.add_format({"num_format": "+0;-0;0"})
    date_fmt = book.add_format({"num_format": 'dd"."mm"."yyyy'})
    missing_fmt = book.add_format({"font_color": "#9B3A32"})
    comparison_formats = {
        "count": book.add_format({"num_format": "0"}),
        "share": book.add_format({"num_format": "0.0%"}),
        "percent": book.add_format({"num_format": "+0.0%;-0.0%;0.0%"}),
        "pp": book.add_format({"num_format": '+0.0" п.п.";-0.0" п.п.";0.0" п.п."'}),
        "text": note,
    }

    main.merge_range("A1:M1", "Динамика рейтингов и оценок", title)
    main.write("A2", f"Период: {report.start:%d.%m.%Y}—{report.end:%d.%m.%Y}")
    if report.previous_start and report.previous_end:
        main.write(
            "O2", f"Сравнение: {report.previous_start:%d.%m.%Y}—{report.previous_end:%d.%m.%Y}"
        )
    main.merge_range(
        "A3:M3",
        "Начало — снимок накануне первого дня; изменение оценок — прирост за период. "
        "Пустая ячейка означает отсутствие снимка, а не ноль.",
        note,
    )
    headings = [
        "Артикул",
        "Код WB",
        "Рейтинг на начало",
        "Рейтинг на конец",
        "Изменение рейтинга",
        "Оценки 5 Δ",
        "Оценки 4 Δ",
        "Оценки 3 Δ",
        "Оценки 2 Δ",
        "Оценки 1 Δ",
        "Тренд рейтинга",
        "Данные",
        "Кабинет",
    ]
    headings += ["Название товара", *[label for label, _, _ in _comparison_cells(None)]]
    main.merge_range(
        "O3:AC3",
        "Изменение количества, % = (сейчас / раньше − 1) × 100. "
        "Доля = оценки категории / все оценки периода; разница долей — п.п. "
        "Выкуп — семидневное окно на конец каждого периода, без суммирования.",
        note,
    )
    main.merge_range(
        "O4:AC4",
        "0 → число: «новое»; 0 → 0: «—»; нет снимка: «Нет данных». "
        "Корректировки WB не заменяются нулём. Пропуски графиков остаются разрывами.",
        note,
    )
    main.set_row(2, 35)
    main.set_row(3, 35)
    header_row = 5
    group_header = book.add_format(
        {"bold": True, "font_color": "#183153", "bg_color": "#DDEBF4", "align": "center"}
    )
    main.merge_range(4, 0, 4, 13, "Товар и показатели выбранного периода", group_header)
    main.write(4, 14, "Рейтинг, %", group_header)
    main.merge_range(4, 15, 4, 17, "Все оценки", group_header)
    for offset, label in enumerate(("5★", "4★", "3★", "2★", "1★", "Негатив 1–3★")):
        first = 18 + offset * 7
        main.merge_range(4, first, 4, first + 6, label, group_header)
    main.merge_range(4, 60, 4, 62, "Выкуп: окно 7 дней", group_header)
    main.merge_range(4, 63, 4, 64, "Качество данных", group_header)
    main.write_row(header_row, 0, headings, header)
    main.freeze_panes(header_row + 1, 2)
    main.set_column("A:A", 20)
    main.set_column("B:B", 16)
    main.set_column("C:E", 20)
    main.set_column("F:J", 14)
    main.set_column("K:K", 24)
    main.set_column("L:L", 18)
    main.set_column("M:M", 18)
    main.set_column(13, 13, 32)
    main.set_column(14, len(headings) - 1, 20)
    main.set_row(header_row, 35)

    series.write("A1", "Дата", header)
    series.write("A2", "Средний рейтинг выбранных артикулов")
    series.set_column("A:A", 36)
    if report.dates:
        series.set_column(1, len(report.dates), 13)
    for index, day in enumerate(report.dates, start=1):
        series.write_datetime(0, index, dt.datetime.combine(day, dt.time.min), date_fmt)
        if not report.rows:
            series.write_blank(1, index, None, rating_fmt)
            continue
        column = xl_col_to_name(index)
        last_product_row = len(report.rows) + 2
        formula = (
            f'=IF(COUNT({column}3:{column}{last_product_row})=0,"",'
            f"AVERAGE({column}3:{column}{last_product_row}))"
        )
        values = [
            value
            for row in report.rows
            if (value := report.ratings_by_day[row.nm_id][index - 1]) is not None
        ]
        average = sum(values) / len(values) if values else ""
        series.write_formula(1, index, formula, rating_fmt, average)

    for offset, row in enumerate(report.rows):
        main_row = header_row + 1 + offset
        series_row = offset + 2
        main.write_string(main_row, 0, row.vendor_code)
        main.write_string(main_row, 1, str(row.nm_id))
        main.write_string(main_row, 12, row.account_key)
        main.write_string(main_row, 13, row.title or "Название не указано")
        for column, (_, comparison_value, kind) in enumerate(_comparison_cells(row.comparison), 14):
            cell_format = comparison_formats[kind]
            if comparison_value is None:
                main.write_blank(main_row, column, None, cell_format)
            elif isinstance(comparison_value, str):
                main.write_string(main_row, column, comparison_value, missing_fmt)
            else:
                main.write_number(main_row, column, comparison_value, cell_format)
        for column, value, cell_format in (
            (2, row.rating_start, rating_fmt),
            (3, row.rating_end, rating_fmt),
            (4, row.rating_delta, delta_fmt),
            (5, row.ratings_5_delta, count_fmt),
            (6, row.ratings_4_delta, count_fmt),
            (7, row.ratings_3_delta, count_fmt),
            (8, row.ratings_2_delta, count_fmt),
            (9, row.ratings_1_delta, count_fmt),
        ):
            if value is None:
                main.write_blank(main_row, column, None, cell_format)
            else:
                main.write_number(main_row, column, value, cell_format)
        if row.rating_start is None or row.rating_end is None:
            main.write_string(main_row, 11, "Нет данных", missing_fmt)
        else:
            main.write_string(main_row, 11, "Полные")

        series.write_string(series_row, 0, row.vendor_code)
        for index, value in enumerate(report.ratings_by_day[row.nm_id], start=1):
            if value is None:
                series.write_blank(series_row, index, None, rating_fmt)
            else:
                series.write_number(series_row, index, value, rating_fmt)
        if report.dates:
            end_column = xl_col_to_name(len(report.dates))
            excel_row = series_row + 1
            main.add_sparkline(
                main_row,
                10,
                {
                    "range": f"'Ряды'!$B${excel_row}:${end_column}${excel_row}",
                    "type": "line",
                    "min": 4.0,
                    "max": 5.0,
                    "series_color": "#2875B8",
                },
            )
        main.set_row(main_row, 24)

    if report.rows:
        main.autofilter(header_row, 0, header_row + len(report.rows), len(headings) - 1)
    if report.dates and report.rows:
        chart = book.add_chart({"type": "line"})
        chart.add_series(
            {
                "name": "Средний рейтинг выбранных артикулов",
                "categories": ["Ряды", 0, 1, 0, len(report.dates)],
                "values": ["Ряды", 1, 1, 1, len(report.dates)],
                "line": {"color": "#2875B8", "width": 2.25},
            }
        )
        chart.set_title({"name": "Средний рейтинг выбранных артикулов"})
        chart.set_y_axis({"min": 4.0, "max": 5.0, "num_format": "0.00"})
        chart.set_x_axis({"date_axis": True, "num_format": 'dd"."mm'})
        chart.set_legend({"none": True})
        chart.show_blanks_as("gap")
        main.insert_chart(header_row + len(report.rows) + 3, 0, chart)

    if report.daily_by_product and report.rows and report.dates:
        _daily_charts(book, report, header, date_fmt)

    book.close()
    return output.getvalue()


def _daily_charts(
    book: xlsxwriter.Workbook, report: RatingReport, header: Format, date_fmt: Format
) -> None:
    """Editable daily counts feed weighted portfolio charts; incomplete days stay blank."""
    daily = book.add_worksheet("Дневные ряды")
    charts = book.add_worksheet("Графики оценок")
    integer = book.add_format({"num_format": "0"})
    pct = book.add_format({"num_format": "0.0%"})
    daily.set_column("A:A", 15)
    daily.set_column("B:J", 18)
    keys = ("total", "negative", "5", "4", "3", "2", "1")
    daily.write_row(
        0,
        0,
        [
            "Дата",
            "Новых оценок",
            "Негативных оценок",
            "Доля негатива",
            "5★",
            "4★",
            "3★",
            "2★",
            "1★",
            "Данные",
        ],
        header,
    )
    daily.freeze_panes(1, 1)
    detail_header = len(report.dates) + 4
    daily.write_row(
        detail_header,
        0,
        [
            "Дата",
            "Кабинет",
            "Артикул",
            "Код WB",
            "Всего",
            "Негатив",
            "5★",
            "4★",
            "3★",
            "2★",
            "1★",
            "Данные",
        ],
        header,
    )
    lookup = {
        nm_id: {point.snapshot_date: point for point in points}
        for nm_id, points in report.daily_by_product.items()
    }
    for index, day in enumerate(report.dates, 1):
        start = detail_header + 1 + (index - 1) * len(report.rows)
        points = [lookup.get(row.nm_id, {}).get(day) for row in report.rows]
        for offset, (product, point) in enumerate(zip(report.rows, points, strict=True)):
            target = start + offset
            daily.write_datetime(target, 0, dt.datetime.combine(day, dt.time.min), date_fmt)
            for column, identifier in enumerate(
                (product.account_key, product.vendor_code, str(product.nm_id)), 1
            ):
                daily.write_string(target, column, identifier)
            for column, key in enumerate(keys, 4):
                value = point.counts[key] if point else None
                if value is None:
                    daily.write_blank(target, column, None, integer)
                else:
                    daily.write_number(target, column, value, integer)
            daily.write_string(target, 11, STATUS_LABELS[point.status] if point else "Нет данных")
        daily.write_datetime(index, 0, dt.datetime.combine(day, dt.time.min), date_fmt)
        valid = all(point and point.status in {"ok", "empty"} for point in points)
        totals: dict[str, int | None] = (
            {key: sum(point.counts[key] or 0 for point in points if point) for key in keys}
            if valid
            else dict.fromkeys(keys)
        )
        first, last = start + 1, start + len(report.rows)
        status_range = f"$L${first}:$L${last}"
        check = (
            f'COUNTIF({status_range},"Полные")+'
            f'COUNTIF({status_range},"Нет оценок")={len(report.rows)}'
        )
        for dest, source, key in (
            (1, "E", "total"),
            (2, "F", "negative"),
            (4, "G", "5"),
            (5, "H", "4"),
            (6, "I", "3"),
            (7, "J", "2"),
            (8, "K", "1"),
        ):
            daily.write_formula(
                index,
                dest,
                f'=IF({check},SUM({source}{first}:{source}{last}),"")',
                integer,
                totals[key] if totals[key] is not None else "",
            )
        total, negative = totals["total"], totals["negative"]
        share_value: float | str = negative / total if total and negative is not None else ""
        daily.write_formula(index, 3, f'=IFERROR(C{index + 1}/B{index + 1},"")', pct, share_value)
        daily.write_string(index, 9, "Полные" if valid else "Нет данных / корректировка")
    specifications = [
        ("Новые оценки по дням", "line", [("Новых оценок", 1)], "Количество, шт."),
        ("Негативные оценки по дням", "line", [("Негативных оценок", 2)], "Количество, шт."),
        ("Доля негативных оценок", "line", [("Доля негатива", 3)], "Доля, %"),
        (
            "Структура новых оценок",
            "column",
            [(f"{star}★", 9 - star) for star in (5, 4, 3, 2, 1)],
            "Количество, шт.",
        ),
    ]
    for index, (title, kind, items, unit) in enumerate(specifications):
        chart = book.add_chart(
            {"type": kind, **({"subtype": "stacked"} if kind == "column" else {})}
        )
        for name, column in items:
            chart.add_series(
                {
                    "name": name,
                    "categories": [daily.name, 1, 0, len(report.dates), 0],
                    "values": [daily.name, 1, column, len(report.dates), column],
                }
            )
        chart.set_title({"name": title})
        chart.set_x_axis({"name": "Дата", "date_axis": True, "num_format": 'dd"."mm'})
        chart.set_y_axis(
            {"name": unit, "min": 0, **({"max": 1, "num_format": "0%"} if index == 2 else {})}
        )
        chart.show_blanks_as("gap")
        charts.insert_chart(index * 17, 0, chart, {"x_scale": 1.6, "y_scale": 1.1})
