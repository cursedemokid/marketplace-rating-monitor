"""Excel-отчёт 2: доля причин среди негативных отзывов по дням."""

from __future__ import annotations

import datetime as dt
from io import BytesIO

import xlsxwriter
from xlsxwriter.utility import xl_col_to_name

from app.repositories.reporting import ReasonReport


def make_reason_workbook(report: ReasonReport) -> bytes:
    """Сформировать доли, объёмы и связанные с ними подробные графики."""
    output = BytesIO()
    book = xlsxwriter.Workbook(output, {"in_memory": True})
    main = book.add_worksheet("Причины")
    counts = book.add_worksheet("Объёмы")

    title = book.add_format({"bold": True, "font_size": 17, "font_color": "#183153"})
    note = book.add_format({"font_color": "#506175", "text_wrap": True, "valign": "top"})
    header = book.add_format(
        {"bold": True, "font_color": "#FFFFFF", "bg_color": "#183153", "text_wrap": True}
    )
    date_fmt = book.add_format({"num_format": 'dd"."mm"."yyyy', "bold": True})
    percent_fmt = book.add_format({"num_format": "0.0%"})
    total_fmt = book.add_format({"num_format": "0.0%", "bold": True, "bg_color": "#DDEBF4"})
    count_fmt = book.add_format({"num_format": "0"})
    reference_label = book.add_format({"bold": True, "bg_color": "#EEF4FA"})
    reference_percent = book.add_format({"num_format": "0.0%", "bg_color": "#EEF4FA"})
    reference_count = book.add_format({"num_format": "0", "bg_color": "#EEF4FA"})

    last_column = max(3, len(report.dates))
    main.merge_range(0, 0, 0, last_column, "Динамика причин негатива", title)
    main.write(1, 0, f"Период: {report.start:%d.%m.%Y}—{report.end:%d.%m.%Y}")
    main.merge_range(
        2,
        0,
        3,
        last_column,
        "Каждая доля — число негативных отзывов с причиной / все негативные отзывы дня. "
        "Например, 20% — у 20 из 100 негативных отзывов есть эта причина. "
        "Отзыв может войти в несколько строк. Общий итог — доля уникальных негативных "
        "отзывов с известной подтверждённой товарной причиной. "
        "«Причина не указана» и внешние жалобы (например, работа ПВЗ) показаны справочно "
        "и в общий итог не входят. "
        "Пустая ячейка означает, что в этот день не было негативных отзывов.",
        note,
    )
    main.set_row(2, 32)
    main.set_row(3, 32)
    main.merge_range(
        4,
        0,
        4,
        last_column,
        "Цвет причин: 0% — зелёный, 50% — жёлтый, 100% — красный. "
        "Голубые строки — справочные, без оценки серьёзности проблемы. "
        "«Общий итог» показывает полноту определения товарных причин.",
        note,
    )
    main.set_column(0, 0, 31)
    if report.dates:
        main.set_column(1, len(report.dates), 14)

    header_row = 5
    main.write(header_row, 0, "Причина", header)
    counts.write(header_row, 0, "Показатель", header)
    counts.set_column(0, 0, 31)
    if report.dates:
        counts.set_column(1, len(report.dates), 14)
    for column, day in enumerate(report.dates, start=1):
        day_value = dt.datetime.combine(day, dt.time.min)
        main.write_datetime(header_row, column, day_value, date_fmt)
        counts.write_datetime(header_row, column, day_value, date_fmt)
    main.freeze_panes(header_row + 1, 1)
    counts.freeze_panes(header_row + 1, 1)

    for offset, row in enumerate(report.rows):
        sheet_row = header_row + 1 + offset
        reference = row.is_external or row.name == "причина не указана"
        label_format = reference_label if reference else None
        share_format = reference_percent if reference else percent_fmt
        amount_format = reference_count if reference else count_fmt
        main.write_string(sheet_row, 0, row.name, label_format)
        counts.write_string(sheet_row, 0, row.name, label_format)
        for column, (share, count) in enumerate(zip(row.shares, row.counts, strict=True), 1):
            if share is None:
                main.write_blank(sheet_row, column, None, share_format)
            else:
                main.write_number(sheet_row, column, share, share_format)
            counts.write_number(sheet_row, column, count, amount_format)

    total_row = header_row + 1 + len(report.rows)
    main.write_string(total_row, 0, "Общий итог", total_fmt)
    counts.write_string(total_row, 0, "С выявленной причиной", header)
    negative_row = total_row + 1
    counts.write_string(negative_row, 0, "Все негативные отзывы", header)
    for column, (share, identified, negative) in enumerate(
        zip(report.total_shares, report.identified_counts, report.negative_counts, strict=True), 1
    ):
        if share is None:
            main.write_blank(total_row, column, None, total_fmt)
        else:
            main.write_number(total_row, column, share, total_fmt)
        counts.write_number(total_row, column, identified, count_fmt)
        counts.write_number(negative_row, column, negative, count_fmt)

    other_row = negative_row + 1
    counts.write_string(other_row, 0, "Без определённой товарной причины", header)
    counts.set_row(other_row, 30)
    for column, (identified, negative) in enumerate(
        zip(report.identified_counts, report.negative_counts, strict=True), 1
    ):
        letter = xl_col_to_name(column)
        counts.write_formula(
            other_row,
            column,
            f"={letter}{negative_row + 1}-{letter}{total_row + 1}",
            count_fmt,
            negative - identified,
        )

    if report.rows and report.dates:
        main.autofilter(header_row, 0, total_row - 1, len(report.dates))
        for offset, row in enumerate(report.rows):
            if row.is_external or row.name == "причина не указана":
                continue
            sheet_row = header_row + 1 + offset
            main.conditional_format(
                sheet_row,
                1,
                sheet_row,
                len(report.dates),
                {
                    "type": "3_color_scale",
                    "min_type": "num",
                    "min_value": 0,
                    "min_color": "#C6EFCE",
                    "mid_type": "num",
                    "mid_value": 0.5,
                    "mid_color": "#FFEB9C",
                    "max_type": "num",
                    "max_value": 1,
                    "max_color": "#FFC7CE",
                },
            )
    if report.dates:
        chart = book.add_chart({"type": "line"})
        chart.add_series(
            {
                "name": "Доля с выявленной причиной",
                "categories": ["Причины", header_row, 1, header_row, len(report.dates)],
                "values": ["Причины", total_row, 1, total_row, len(report.dates)],
                "line": {"color": "#2875B8", "width": 2.25},
            }
        )
        chart.set_title({"name": "Негативные отзывы с выявленной причиной"})
        chart.set_y_axis({"min": 0, "max": 1, "num_format": "0%"})
        chart.set_x_axis({"date_axis": True, "num_format": 'dd"."mm'})
        chart.set_legend({"none": True})
        main.insert_chart(total_row + 3, 0, chart)

        _add_charts_sheet(book, report, header_row, total_row, negative_row, other_row)

    book.close()
    return output.getvalue()


def _add_charts_sheet(
    book: xlsxwriter.Workbook,
    report: ReasonReport,
    header_row: int,
    total_row: int,
    negative_row: int,
    other_row: int,
) -> None:
    """Обзор причин без суммирования пересекающихся меток отзывов."""
    sheet = book.add_worksheet("Графики")
    sheet.hide_gridlines(2)
    sheet.set_column("A:U", 10)
    sheet.set_column("E:F", 14)
    sheet.set_default_row(19)
    sheet.freeze_panes(6, 0)
    title = book.add_format({"bold": True, "font_size": 18, "font_color": "#183153"})
    note = book.add_format({"text_wrap": True, "valign": "top", "font_color": "#506175"})
    header = book.add_format({"bold": True, "bg_color": "#183153", "font_color": "white"})
    number = book.add_format({"num_format": "#,##0"})
    percent = book.add_format({"num_format": "0.0%"})
    wrapped = book.add_format({"text_wrap": True, "valign": "vcenter"})
    sheet.merge_range("A1:U1", "Причины и объём негативных отзывов", title)
    sheet.merge_range("A2:U2", f"Период: {report.start:%d.%m.%Y}—{report.end:%d.%m.%Y}")
    sheet.merge_range(
        "A3:U4",
        "Доля причины = отзывы с этой причиной / все отзывы с оценками 1–3 за тот же период. "
        "Доли за период считаются по общему числу отзывов, а не среднему дневных процентов. "
        "Метки могут пересекаться: их суммы не равны числу уникальных отзывов. "
        "В частые причины не входят пустые, нечитаемые отзывы, неуказанная причина "
        "и ошибочно низкая оценка.",
        note,
    )
    negative_total = sum(report.negative_counts)
    last_column = xl_col_to_name(len(report.dates))
    sheet.merge_range("A5:D5", "Негативных отзывов за период")
    sheet.write_formula(
        "E5",
        f"=SUM('Объёмы'!B{negative_row + 1}:{last_column}{negative_row + 1})",
        number,
        negative_total,
    )
    # Выбор причин фиксируется при экспорте; значения и доли связаны с детализацией.
    non_complaints = {
        "пустой отзыв",
        "причина не указана",
        "нечитаемый отзыв",
        "ошибочно низкая оценка",
    }
    ranked = sorted(
        (
            (offset, row)
            for offset, row in enumerate(report.rows)
            if not row.is_external and row.name not in non_complaints and sum(row.counts) > 0
        ),
        key=lambda item: (-sum(item[1].counts), item[1].name),
    )[:8]
    sheet.merge_range("A48:F48", "Частые причины за период", header)
    sheet.merge_range("A49:D49", "Причина", header)
    sheet.write("E49", "Количество", header)
    sheet.write("F49", "От негатива", header)
    for index, (offset, row) in enumerate(ranked):
        output_row = 49 + index
        source_row = header_row + 2 + offset  # Excel row on both detail sheets.
        sheet.merge_range(output_row, 0, output_row, 3, row.name, wrapped)
        sheet.set_row(output_row, 30)
        count = sum(row.counts)
        sheet.write_formula(
            output_row,
            4,
            f"=SUM('Объёмы'!B{source_row}:{last_column}{source_row})",
            number,
            count,
        )
        sheet.write_formula(
            output_row,
            5,
            f'=IF($E$5=0,"",E{output_row + 1}/$E$5)',
            percent,
            count / negative_total if negative_total else "",
        )

    def configure(chart: xlsxwriter.chart.Chart, chart_title: str, *, daily: bool) -> None:
        chart.set_title({"name": chart_title, "name_font": {"size": 13}})
        chart.set_size({"width": 720, "height": 350})
        chart.set_chartarea({"border": {"none": True}})
        chart.set_legend({"position": "bottom", "font": {"size": 10}})
        chart.show_blanks_as("gap")
        if daily:
            chart.set_x_axis({"date_axis": True, "num_format": 'dd"."mm', "num_font": {"size": 10}})

    volume = book.add_chart({"type": "column", "subtype": "stacked"})
    for volume_row, label, color in (
        (total_row, "С определённой товарной причиной", "#2875B8"),
        (other_row, "Без определённой товарной причины", "#B4BEC9"),
    ):
        volume.add_series(
            {
                "name": label,
                "categories": ["Объёмы", header_row, 1, header_row, len(report.dates)],
                "values": ["Объёмы", volume_row, 1, volume_row, len(report.dates)],
                "fill": {"color": color},
                "border": {"none": True},
            }
        )
    configure(volume, "Негативные отзывы по дням, шт.", daily=True)
    volume.set_y_axis({"min": 0, "num_format": "0"})
    sheet.insert_chart("L27", volume)
    if not ranked:
        sheet.merge_range(
            "A7:J8", "За период нет отзывов с определёнными товарными причинами.", note
        )
        return

    top = book.add_chart({"type": "bar"})
    top.add_series(
        {
            "name": "Доля от всех негативных отзывов",
            "categories": ["Графики", 49, 0, 48 + len(ranked), 0],
            "values": ["Графики", 49, 5, 48 + len(ranked), 5],
            "fill": {"color": "#2875B8"},
            "border": {"none": True},
            "data_labels": {"value": True, "num_format": "0.0%"},
        }
    )
    configure(top, "Частые причины за период, доля от негатива", daily=False)
    top.set_legend({"none": True})
    top.set_x_axis({"min": 0, "num_format": "0%"})
    top.set_y_axis({"reverse": True, "num_font": {"size": 10}})
    top.set_plotarea({"layout": {"x": 0.44, "y": 0.15, "width": 0.5, "height": 0.75}})
    sheet.insert_chart("A7", top)

    colors = ("#2875B8", "#9A5B9C", "#D07A30", "#268C8A", "#7C6B47")
    shares = book.add_chart({"type": "line"})
    amounts = book.add_chart({"type": "column"})
    for index, (offset, row) in enumerate(ranked[:5]):
        source_row = header_row + 1 + offset  # Zero-based chart row.
        series = {
            "name": row.name,
            "categories": ["Причины", header_row, 1, header_row, len(report.dates)],
            "values": ["Причины", source_row, 1, source_row, len(report.dates)],
            "line": {"color": colors[index], "width": 2},
        }
        shares.add_series(series)
        if index < 3:
            amounts.add_series(
                {
                    "name": row.name,
                    "categories": series["categories"],
                    "values": ["Объёмы", source_row, 1, source_row, len(report.dates)],
                    "fill": {"color": colors[index]},
                    "border": {"none": True},
                }
            )
    configure(shares, "Доли пяти частых причин по дням", daily=True)
    shares.set_y_axis({"min": 0, "num_format": "0%"})
    sheet.insert_chart("L7", shares)
    configure(amounts, "Отзывы по трём частым причинам, шт.", daily=True)
    amounts.set_y_axis({"min": 0, "num_format": "0"})
    sheet.insert_chart("A27", amounts)
