"""Browser-level acceptance checks against the isolated E2E server and database."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from playwright.sync_api import Page, expect

TABS = [
    ("Требуют внимания", "Последние сигналы по артикулам за выбранный период."),
    ("Все артикулы", "Все товары под наблюдением."),
    ("История сигналов", "Записи о сигналах и результате отправки"),
    ("Выгрузки", "Скачайте Excel"),
    ("Пороги сигналов", "Пороги задают условия создания сигналов"),
    ("Номенклатура", "Здесь администратор сверяет XLSX"),
    ("AI и модерация", "Расходы запросов AI"),
]


def test_login_rejects_invalid_credentials_and_logout_returns_to_login(
    page: Page, base_url: str, admin_REDACTED: str
) -> None:
    page.goto(base_url)
    page.get_by_label("Логин").fill("e2eadmin")
    page.get_by_label("Пароль").fill("неверный пароль")
    page.get_by_role("button", name="Войти").click()
    expect(page.get_by_role("alert")).to_contain_text("Неверные учётные данные")

    page.get_by_label("Пароль").fill(admin_REDACTED)
    page.get_by_role("button", name="Войти").click()
    expect(page.get_by_role("navigation", name="Разделы")).to_be_visible()
    page.get_by_role("button", name="Выйти").click()
    expect(page.get_by_role("heading", name="Вход")).to_be_visible()


@pytest.mark.parametrize(("tab", "help_text"), TABS)
def test_admin_can_open_each_section_and_read_its_help(
    admin_page: Page, tab: str, help_text: str
) -> None:
    admin_page.get_by_role("button", name=tab, exact=True).click()
    expect(admin_page.get_by_role("heading", name=tab, exact=True)).to_be_visible()
    admin_page.get_by_text("Что здесь показано", exact=True).click()
    expect(admin_page.get_by_text(help_text, exact=False)).to_be_visible()


def test_ratings_table_shows_exact_seed_values_and_null_title_placeholder(admin_page: Page) -> None:
    admin_page.get_by_role("button", name="Все артикулы", exact=True).click()
    main = admin_page.get_by_role("row").filter(has_text="E2E-MAIN")
    expect(main).to_contain_text("Тестовые подгузники")
    expect(main).to_contain_text("4,80")
    expect(main).to_contain_text("4,79")
    expect(main).to_contain_text("-0,01")
    expect(main).to_contain_text("+7 / +1 / 0 / 0 / +2")
    expect(admin_page.get_by_text("Предыдущий период:", exact=False)).to_be_visible()
    expect(admin_page.get_by_role("columnheader", name="доля Δ п.п.").first).to_be_visible()
    expect(admin_page.get_by_role("columnheader", name="Всего оценок, пред.")).to_be_visible()
    expect(admin_page.get_by_role("columnheader", name="Всего Δ %")).to_be_visible()
    expect(main.locator("td").nth(9)).to_have_text("8")
    expect(main.locator("td").nth(10)).to_have_text("10")
    expect(main.locator("td").nth(11)).to_have_text("+25,0%")
    expect(main).to_contain_text("новое")
    expect(main).to_contain_text("—")
    expect(main).not_to_contain_text(re.compile(r"Нет данных\s*%\s*(?:новое|—)"))
    expect(admin_page.get_by_role("columnheader", name="Выкуп", exact=True)).to_be_visible()

    no_title = admin_page.get_by_role("row").filter(has_text="E2E-NO-TITLE")
    expect(no_title).to_contain_text("Название не указано")
    expect(no_title.locator("td").nth(-3)).to_have_text("Нет данных")
    expect(no_title.locator("td").nth(-2)).to_have_text("90,0")
    expect(no_title.locator("td").nth(-1)).to_have_text("Нет данных")
    admin_page.get_by_text("Как читать рейтинг и оценки", exact=True).click()
    expect(
        admin_page.get_by_text(
            "Относительное изменение = (текущее − предыдущее) / предыдущее × 100%", exact=False
        )
    ).to_be_visible()


def test_late_attention_response_cannot_replace_selected_products_page(admin_page: Page) -> None:
    admin_page.evaluate("""() => {
      const fetchOriginal = window.fetch.bind(window);
      window.__attentionGate = { started: false, completed: false };
      window.fetch = async (input, init) => {
        const response = await fetchOriginal(input, init);
        const url = typeof input === "string" ? input : input.url;
        if (url.includes("/api/attention?")) {
          window.__attentionGate.started = true;
          await new Promise(resolve => { window.__attentionGate.release = resolve; });
          await new Promise(resolve => setTimeout(resolve, 0));
          window.__attentionGate.completed = true;
        }
        return response;
      };
    }""")
    admin_page.get_by_role("button", name="Требуют внимания", exact=True).click()
    admin_page.wait_for_function("window.__attentionGate.started === true")
    admin_page.get_by_role("button", name="Все артикулы", exact=True).click()
    expect(admin_page.get_by_role("row").filter(has_text="E2E-MAIN")).to_be_visible()
    admin_page.evaluate("window.__attentionGate.release()")
    admin_page.wait_for_function("window.__attentionGate.completed === true")
    expect(admin_page.get_by_role("heading", name="Все артикулы", exact=True)).to_be_visible()
    expect(admin_page.get_by_role("row").filter(has_text="E2E-MAIN")).to_be_visible()


def test_product_search_and_cabinet_filter_select_expected_rows(admin_page: Page) -> None:
    admin_page.get_by_role("button", name="Все артикулы", exact=True).click()
    admin_page.get_by_text("Фильтры товаров", exact=True).click()
    admin_page.get_by_role("combobox", name=re.compile("^Кабинет")).select_option("DemoSeller")
    admin_page.get_by_role("button", name="Показать", exact=True).click()
    expect(admin_page.get_by_role("row").filter(has_text="E2E-OTHER")).to_be_visible()
    expect(admin_page.get_by_role("row").filter(has_text="E2E-MAIN")).to_have_count(0)

    admin_page.get_by_role("button", name="Сбросить фильтры", exact=True).click()
    admin_page.get_by_label("Быстрый поиск").fill("Тестовые салфетки")
    admin_page.get_by_role("button", name="Показать", exact=True).click()
    expect(admin_page.get_by_role("row").filter(has_text="E2E-OTHER")).to_be_visible()
    expect(admin_page.get_by_role("row").filter(has_text="E2E-MAIN")).to_have_count(0)


def test_admin_can_add_catalog_item_and_already_monitored_item_is_disabled(
    admin_page: Page,
) -> None:
    admin_page.get_by_role("button", name="Все артикулы", exact=True).click()
    admin_page.get_by_role("button", name="Добавить товары", exact=True).click()
    dialog = admin_page.get_by_role("dialog", name="Добавить товары в мониторинг")
    expect(dialog.get_by_text("Тестовые подгузники", exact=True)).to_be_visible()
    monitored_checkbox = dialog.get_by_role("checkbox", name=re.compile(r"WB 999921001"))
    expect(monitored_checkbox).to_be_disabled()

    dialog.get_by_label("Поиск по названию, артикулу или коду WB").fill("Товар для добавления")
    expect(dialog.get_by_text("Товар для добавления", exact=True)).to_be_visible()
    add_checkbox = dialog.get_by_role("checkbox", name=re.compile(r"WB 999921003"))
    expect(add_checkbox).to_be_enabled()
    add_checkbox.check()
    dialog.get_by_role("button", name="Добавить выбранные", exact=True).click()
    expect(dialog).to_be_hidden()
    expect(admin_page.get_by_role("row").filter(has_text="E2E-ADD")).to_be_visible()


def test_viewer_hides_admin_sections_and_catalog_controls_and_gets_http_403(
    page: Page, base_url: str, viewer_page: Page, viewer_REDACTED: str
) -> None:
    viewer_page.get_by_role("button", name="Все артикулы", exact=True).click()
    expect(viewer_page.get_by_role("button", name="Добавить товары", exact=True)).to_have_count(0)
    expect(viewer_page.get_by_role("button", name="Номенклатура", exact=True)).to_have_count(0)
    expect(viewer_page.get_by_role("button", name="AI и модерация", exact=True)).to_have_count(0)

    page.goto(base_url)
    page.get_by_label("Логин").fill("e2eviewer")
    page.get_by_label("Пароль").fill(viewer_REDACTED)
    with page.expect_response(
        lambda response: response.url.endswith("/api/login")
    ) as login_response:
        page.get_by_role("button", name="Войти").click()
    token = login_response.value.json()["access_token"]
    anonymous = page.request.get(f"{base_url}/api/products/catalog")
    assert anonymous.status == 401
    response = page.request.get(
        f"{base_url}/api/products/catalog", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status == 403


@pytest.mark.parametrize(
    ("report", "button_name"), [("ratings", "Скачать отчёт 1"), ("reasons", "Скачать отчёт 2")]
)
def test_filtered_exports_download_valid_xlsx_with_expected_product(
    admin_page: Page, report: str, button_name: str, tmp_path: Path
) -> None:
    admin_page.get_by_role("button", name="Все артикулы", exact=True).click()
    admin_page.get_by_label("Быстрый поиск").fill("E2E-MAIN")
    with admin_page.expect_response(
        lambda response: "/api/reports/ratings?" in response.url and "q=E2E-MAIN" in response.url
    ) as report_response:
        admin_page.get_by_role("button", name="Показать", exact=True).click()
    report_data = report_response.value.json()
    assert [row["vendor_code"] for row in report_data["rows"]] == ["E2E-MAIN"]
    comparison = report_data["rows"][0]["comparison"]
    expect(admin_page.locator(".comparison-table tbody tr td").nth(11)).to_have_text("+25,0%")
    admin_page.get_by_role("button", name="Выгрузки", exact=True).click()
    with admin_page.expect_download() as download_info:
        admin_page.get_by_role("button", name=button_name, exact=True).click()
    download_path = tmp_path / f"{report}.xlsx"
    download_info.value.save_as(download_path)
    assert zipfile.is_zipfile(download_path)
    with zipfile.ZipFile(download_path) as workbook:
        members = workbook.namelist()
        assert "xl/workbook.xml" in members
        assert any(name.startswith("xl/worksheets/sheet") for name in members)
        workbook_text = " ".join(
            workbook.read(name).decode("utf-8", errors="ignore")
            for name in members
            if name.endswith(".xml")
        )
        if report == "ratings":
            assert "E2E-MAIN" in workbook_text
            assert "E2E-OTHER" not in workbook_text
            assert "E2E-NO-TITLE" not in workbook_text
            assert "E2E-ADD" not in workbook_text
            ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            strings = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
            shared = ["".join(item.itertext()) for item in strings]
            sheet = ET.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
            rows = {row.get("r"): row for row in sheet.findall(".//x:row", ns)}
            columns = {
                shared[int(cell.findtext("x:v", namespaces=ns))]: cell.get("r").rstrip("3228787319")
                for cell in rows["6"].findall("x:c", ns)
                if cell.get("t") == "s"
            }
            cells = {cell.get("r"): cell for cell in rows["7"].findall("x:c", ns)}
            for label, expected in (
                (
                    "5★: изменение количества, %",
                    comparison["count_changes_pct"]["5"]["value"] / 100,
                ),
                ("5★: изменение доли, п.п.", comparison["share_changes_pp"]["5"]["value"]),
                ("Изменение выкупа, п.п.", comparison["buyout_delta_pp"]["value"]),
            ):
                assert float(
                    cells[columns[label] + "7"].findtext("x:v", namespaces=ns)
                ) == pytest.approx(expected)
        else:
            # Reasons aggregate products. The other cabinet has another negative
            # review: a missing filter would incorrectly give two instead of one.
            ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            strings = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
            shared = ["".join(item.itertext()) for item in strings]
            counts = ET.fromstring(workbook.read("xl/worksheets/sheet2.xml"))
            negative_rows = []
            for row in counts.findall(".//x:row", ns):
                cells = row.findall("x:c", ns)
                if not cells or cells[0].get("t") != "s":
                    continue
                label = cells[0].findtext("x:v", namespaces=ns)
                if shared[int(label)] == "Все негативные отзывы":
                    negative_rows.append(cells)
            assert len(negative_rows) == 1
            values = [cell.findtext("x:v", namespaces=ns) for cell in negative_rows[0][1:]]
            assert values == ["0"] * 6 + ["1"]
    expect(admin_page.get_by_role("status")).to_contain_text("Файл скачан")


def test_admin_can_save_signal_setting(admin_page: Page) -> None:
    admin_page.get_by_role("button", name="Пороги сигналов", exact=True).click()
    rule = admin_page.locator('[data-rule="rating_change"]')
    threshold = rule.get_by_label("Порог, пункты рейтинга", exact=True)
    original = threshold.input_value()
    changed = "0.37" if original != "0.37" else "0.38"
    threshold.fill(changed)
    rule.get_by_role("button", name="Сохранить", exact=True).click()
    expect(rule.get_by_role("status")).to_contain_text("Сохранено")
    admin_page.get_by_role("button", name="Все артикулы", exact=True).click()
    admin_page.get_by_role("button", name="Пороги сигналов", exact=True).click()
    expect(threshold).to_have_value(changed)
    threshold.fill(original)
    rule.get_by_role("button", name="Сохранить", exact=True).click()
    expect(rule.get_by_role("status")).to_contain_text("Сохранено")


def test_viewer_signal_settings_are_read_only(viewer_page: Page) -> None:
    viewer_page.get_by_role("button", name="Пороги сигналов", exact=True).click()
    expect(viewer_page.get_by_text("У вас доступ для просмотра", exact=False)).to_be_visible()
    expect(viewer_page.get_by_role("button", name="Сохранить", exact=True)).to_have_count(0)
    expect(viewer_page.get_by_label("Порог, пункты рейтинга", exact=True)).to_be_disabled()


def test_nomenclature_lookup_finds_seeded_product(admin_page: Page) -> None:
    admin_page.get_by_role("button", name="Номенклатура", exact=True).click()
    admin_page.get_by_label("Артикул продавца", exact=True).fill("E2E-MAIN")
    admin_page.get_by_role("button", name="Открыть", exact=True).click()
    editor = admin_page.get_by_role("status")
    expect(editor.get_by_label("Пачка", exact=True)).to_have_value("1")
    expect(editor.get_by_label("Категория", exact=True)).to_have_value("Детская гигиена")
    expect(editor.get_by_label("Линейка", exact=True)).to_have_value("Test")


def test_ai_costs_show_seeded_settled_usage_and_status_legend(admin_page: Page) -> None:
    admin_page.get_by_role("button", name="AI и модерация", exact=True).click()
    expect(admin_page.get_by_role("heading", name="Расходы AI за", exact=False)).to_be_visible()
    settled_row = admin_page.get_by_role("row").filter(has_text="Оплачено по факту")
    expect(settled_row).to_contain_text(re.compile(r"30[\s\u00a0\u202f]146"))
    expect(settled_row).to_contain_text("999")
    admin_page.get_by_text("Что означают столбцы и статусы", exact=True).click()
    expect(admin_page.get_by_text("Расход неизвестен (uncertain)", exact=False)).to_be_visible()


def test_product_card_shows_seeded_name_rating_cause_and_charts(admin_page: Page) -> None:
    admin_page.get_by_role("button", name="Все артикулы", exact=True).click()
    admin_page.get_by_role("button", name="E2E-MAIN", exact=True).click()
    expect(
        admin_page.get_by_role("heading", name="Тестовые подгузники", exact=True)
    ).to_be_visible()
    expect(admin_page.get_by_role("img", name="Динамика рейтинга")).to_be_visible()
    expect(admin_page.get_by_role("img", name="Динамика выкупа")).to_be_visible()
    for label in ("Новые оценки всего", "Новые негативные оценки", "Доля негатива"):
        expect(admin_page.get_by_role("img", name=label)).to_be_visible()
    expect(admin_page.get_by_role("img", name="Структура новых оценок по звёздам")).to_be_visible()
    expect(admin_page.locator(".chart-values")).to_have_count(6)
    expect(admin_page.locator(".chart-values caption").first).to_contain_text("значения по дням")
    expect(
        admin_page.locator(".chart-values").first.locator("td", has_text="Нет данных").first
    ).to_be_visible()
    expect(
        admin_page.locator(".chart-values").filter(has_text="Корректировка WB").first
    ).to_be_visible()
    comparison = admin_page.locator(".product-comparison")
    expect(comparison.get_by_role("heading", name="Сравнение периодов")).to_be_visible()
    expect(comparison.get_by_text("Текущий период:", exact=False)).to_be_visible()
    expect(comparison.get_by_text("Предыдущий период:", exact=False)).to_be_visible()
    expect(comparison.get_by_role("row")).to_have_count(8)
    assert admin_page.locator(".line-chart polyline").count() >= 6
    expect(admin_page.get_by_role("heading", name="Причины негатива", exact=False)).to_be_visible()
    causes = admin_page.locator("section").filter(
        has=admin_page.get_by_role("heading", name="Причины негатива", exact=False)
    )
    expect(causes.get_by_role("cell", name="протекают", exact=True)).to_be_visible()
    expect(causes.get_by_role("cell", name="100,0%", exact=True)).to_be_visible()


def test_mobile_catalog_action_is_reachable(admin_page: Page) -> None:
    admin_page.set_viewport_size({"width": 390, "height": 844})
    admin_page.get_by_role("button", name="Все артикулы", exact=True).click()
    table = admin_page.locator(".comparison-table")
    expect(table).to_be_visible()
    assert admin_page.locator(".table-wrap").first.evaluate("el => el.scrollWidth > el.clientWidth")
    add_button = admin_page.get_by_role("button", name="Добавить товары", exact=True)
    expect(add_button).to_be_visible()
    add_button.scroll_into_view_if_needed()
    add_button.click()
    expect(admin_page.get_by_role("dialog", name="Добавить товары в мониторинг")).to_be_visible()
