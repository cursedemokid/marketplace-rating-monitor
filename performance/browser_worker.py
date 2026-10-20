"""Five isolated users coordinate six simultaneous 90-day report samples."""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


def _allowed(url: str, origin: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme in {"data", "blob"} or f"{parts.scheme}://{parts.netloc}" == origin


def _report_response(response, start_date: str, end_date: str) -> bool:
    parts = urlsplit(response.url)
    query = dict(item.split("=", 1) for item in parts.query.split("&") if "=" in item)
    return (
        parts.path == "/api/reports/ratings"
        and query.get("start") == start_date
        and query.get("end") == end_date
    )


def _has_complete_boundaries(payload: dict[str, object], *, expected_rows: int = 1000) -> bool:
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) != expected_rows:
        return False
    for row in rows:
        if not isinstance(row, dict):
            return False
        comparison = row.get("comparison")
        if not isinstance(comparison, dict):
            return False
        current = comparison.get("current")
        previous = comparison.get("previous")
        if (
            not isinstance(current, dict)
            or current.get("status") != "ok"
            or not isinstance(previous, dict)
            or previous.get("status") != "ok"
        ):
            return False
    return True


def main() -> None:
    if len(sys.argv) not in {4, 5} or (len(sys.argv) == 5 and sys.argv[4] != "--cards"):
        raise SystemExit("usage: browser_worker BASE_URL USERNAME WORKER_STATE_DIR [--cards]")
    base_url, username, state_arg = sys.argv[1:4]
    include_cards = len(sys.argv) == 5
    user_number = username.removeprefix("perfuser")
    REDACTED = os.environ[f"WB_PERF_USER_{user_number}_PASSWORD"]
    worker_id = int(user_number)
    state_dir = Path(state_arg)
    origin_parts = urlsplit(base_url)
    origin = f"{origin_parts.scheme}://{origin_parts.netloc}"

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        context.route(
            "**/*",
            lambda route: (
                route.continue_() if _allowed(route.request.url, origin) else route.abort()
            ),
        )
        page = context.new_page()
        page.goto(base_url, wait_until="domcontentloaded")
        page.get_by_label("Логин").fill(username)
        page.get_by_label("Пароль").fill(REDACTED)
        page.get_by_role("button", name="Войти").click()
        page.get_by_role("navigation", name="Разделы").wait_for()
        page.get_by_role("button", name="Все артикулы").click()
        page.locator("table tbody tr").first.wait_for(timeout=120_000)
        end_date = dt.date.fromisoformat(os.environ["WB_PERF_RUN_DATE"])
        start_day = end_date - dt.timedelta(days=89)
        start_date = start_day.isoformat()
        end_value = end_date.isoformat()
        page.locator("#end-date").fill(end_value)
        page.locator("#start-date").fill(start_date)

        (state_dir / f"ready-{worker_id}").write_text("ready", encoding="utf-8")
        values: list[float] = []
        for round_id in range(1, 7):
            go_file = state_dir / f"go-{round_id}"
            deadline = time.monotonic() + 180
            while not go_file.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"worker {worker_id} timed out at barrier {round_id}")
                time.sleep(0.05)
            started = time.perf_counter()
            with page.expect_response(
                lambda response: _report_response(response, start_date, end_value), timeout=120_000
            ) as response_info:
                page.locator("#apply-period").click(timeout=20_000)
            response = response_info.value
            if response.status != 200 or not _has_complete_boundaries(response.json()):
                raise RuntimeError(
                    f"worker {worker_id} report needs 1000 products with both period boundaries"
                )
            page.wait_for_function(
                """() => new Promise(resolve => requestAnimationFrame(() =>
                    requestAnimationFrame(() => resolve(
                      document.querySelectorAll('#content table tbody tr').length === 1000))))""",
                timeout=120_000,
            )
            rows = page.locator("#content table tbody tr")
            period_note = page.locator(".period-note").inner_text()
            start_text = start_day.strftime("%d.%m.%Y")
            end_text = end_date.strftime("%d.%m.%Y")
            if rows.count() != 1000 or start_text not in period_note or end_text not in period_note:
                raise RuntimeError(f"worker {worker_id} rendered an incomplete 90-day report")
            values.append(time.perf_counter() - started)
            (state_dir / f"done-{worker_id}-{round_id}").write_text(
                json.dumps({"worker": worker_id, "round": round_id, "seconds": values[-1]}),
                encoding="utf-8",
            )
        (state_dir / f"result-{worker_id}").write_text(json.dumps(values), encoding="utf-8")
        if include_cards:
            page.locator('[data-product="710000001"]').click()
            page.locator("#content .product-comparison").wait_for(timeout=120_000)
        card_values: list[float] = []
        start_text = start_day.strftime("%d.%m.%Y")
        end_text = end_date.strftime("%d.%m.%Y")
        for round_id in range(1, 7) if include_cards else ():
            go_file = state_dir / f"card-go-{round_id}"
            deadline = time.monotonic() + 180
            while not go_file.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"worker {worker_id} timed out at product-card barrier {round_id}"
                    )
                time.sleep(0.05)
            started = time.perf_counter()
            with (
                page.expect_response(
                    lambda response: (
                        _report_response(response, start_date, end_value)
                        and "nm_id=710000001" in response.url
                    ),
                    timeout=120_000,
                ) as ratings_info,
                page.expect_response(
                    lambda response: (
                        urlsplit(response.url).path == "/api/products/710000001/snapshots"
                        and f"start={start_date}" in response.url
                        and f"end={end_value}" in response.url
                    ),
                    timeout=120_000,
                ) as snapshot_info,
            ):
                page.locator("#apply-period").click(timeout=20_000)
            if (
                ratings_info.value.status != 200
                or snapshot_info.value.status != 200
                or not _has_complete_boundaries(ratings_info.value.json(), expected_rows=1)
            ):
                raise RuntimeError(f"worker {worker_id} product-card report failed")
            page.wait_for_function(
                """({startText,endText}) => {
                  const content = document.querySelector('#content');
                  const tables = [...content.querySelectorAll('.chart-values table')];
                  const note = content.querySelector('.product-comparison p')?.innerText || '';
                  return content.querySelectorAll('.chart-values').length === 6
                    && tables.length === 6 && tables.every(t => t.tBodies[0]?.rows.length === 90)
                    && content.querySelectorAll('.line-chart').length >= 6
                    && content.querySelectorAll('.product-comparison tbody tr').length === 7
                    && note.includes(startText) && note.includes(endText);
                }""",
                arg={"startText": start_text, "endText": end_text},
                timeout=120_000,
            )
            card_values.append(time.perf_counter() - started)
            (state_dir / f"card-done-{worker_id}-{round_id}").write_text(
                json.dumps({"worker": worker_id, "round": round_id, "seconds": card_values[-1]}),
                encoding="utf-8",
            )
        if include_cards:
            (state_dir / f"card-result-{worker_id}").write_text(
                json.dumps(card_values), encoding="utf-8"
            )
        context.close()
        browser.close()


if __name__ == "__main__":
    main()
