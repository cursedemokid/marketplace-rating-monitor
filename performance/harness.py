"""Reproducible local PostgreSQL, collection, API and browser benchmark.

The harness creates a uniquely named local database and deletes only that
database after a run. WB calls use the real WbClient over MockTransport.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import importlib.util
import json
import math
import os
import platform
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
PERF_DB_PATTERN = __import__("re").compile(r"wb_e2e_[0-9a-f]{32}\Z")
GOODS = 1000
HISTORY_DAYS = 181
STORED_FEEDBACKS = 500_000
NEW_FEEDBACKS = 30_000
TARGET_SECONDS = 7200
ACTIVE_CHILDREN: set[subprocess.Popen[str]] = set()


class HarnessError(RuntimeError):
    """Safe failure emitted by the performance runner."""


def percentile(values: list[float], percentage: float) -> float:
    """Nearest-rank percentile; reject empty samples and invalid percentages."""
    if not values:
        raise ValueError("percentile requires at least one measurement")
    if not 0 < percentage <= 100:
        raise ValueError("percentage must be in (0, 100]")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * percentage / 100) - 1)]


def _has_complete_boundaries(payload: dict[str, object], *, expected_rows: int = GOODS) -> bool:
    rows = payload.get("rows")

    def has_ok_boundaries(row: object) -> bool:
        if not isinstance(row, dict):
            return False
        comparison = row.get("comparison")
        if not isinstance(comparison, dict):
            return False
        current = comparison.get("current")
        previous = comparison.get("previous")
        return (
            isinstance(current, dict)
            and current.get("status") == "ok"
            and isinstance(previous, dict)
            and previous.get("status") == "ok"
        )

    return (
        isinstance(rows, list)
        and len(rows) == expected_rows
        and all(has_ok_boundaries(row) for row in rows)
    )


def validate_admin_url(value: str) -> None:
    parts = urlsplit(value)
    if (
        parts.scheme.split("+", 1)[0] not in {"postgres", "postgresql"}
        or parts.hostname not in {"127.0.0.1", "localhost"}
        or not parts.path.strip("/")
        or parts.query
        or parts.fragment
    ):
        raise HarnessError("PostgreSQL admin URL must name a database on loopback")


def _e2e_runner():
    """Import only the E2E runner's guarded DB and child lifecycle helpers."""
    spec = importlib.util.spec_from_file_location("perf_e2e_runner", ROOT / "scripts/run_e2e.py")
    if spec is None or spec.loader is None:
        raise HarnessError("Could not load the isolated E2E lifecycle helpers")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _log(message: str) -> None:
    print(f"[{datetime.now(UTC).isoformat(timespec='seconds')}] {message}", flush=True)


def _system_info() -> dict[str, object]:
    info: dict[str, object] = {
        "platform": platform.platform(),
        "python": sys.version,
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
    }
    try:
        import psutil  # type: ignore[import-not-found]

        info["ram_bytes"] = psutil.virtual_memory().total
        info["cpu_model"] = platform.processor()
    except ImportError:
        try:
            info["ram_bytes"] = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (AttributeError, OSError, ValueError):
            info["ram_bytes"] = None
        info["cpu_model"] = platform.processor() or None
    try:
        import sqlalchemy

        info["sqlalchemy"] = sqlalchemy.__version__
    except Exception:
        pass
    usage = shutil.disk_usage(ROOT)
    info["disk_bytes"] = {"total": usage.total, "used": usage.used, "free": usage.free}
    return info


async def _seed_database(
    database_url: str, run_date: dt.date, env: dict[str, str]
) -> dict[str, object]:
    import asyncpg
    from app.migrate import to_asyncpg_dsn

    _validate_database_target(database_url, env)
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url), timeout=30)
    try:
        # The two accounts receive disjoint product ranges. Every SQL insert is
        # set based, avoiding 681k Python round trips or ORM object creation.
        await conn.execute(
            """insert into wb.products(nm_id,vendor_code,title,account_key,is_monitored,updated_at)
            select 710000000 + n, 'PERF-' || n, 'Benchmark product ' || n,
                   case when n <= 500 then 'default' else 'DemoSeller' end, true, now()
            from generate_series(1, $1::int) n""",
            GOODS,
        )
        await conn.execute(
            """insert into wb.feedbacks
            (id,nm_id,valuation,text,pros,cons,created_at,created_date,imported_at)
            select 'perf-old-' || n,
                   710000001 + ((n - 1) / ($4::int / $1::int)),
                   case when ((n - 1) % ($4::int / $1::int)) % 10 = 0 then 2 else 5 end,
                   'Historical benchmark feedback ' || n, '', '',
                   ($2::date - ($3::int - 1) +
                    floor(((n - 1) % ($4::int / $1::int)) * $3::numeric /
                          ($4::int / $1::int))::int)::timestamp at time zone 'UTC',
                   ($2::date - ($3::int - 1) +
                    floor(((n - 1) % ($4::int / $1::int)) * $3::numeric /
                          ($4::int / $1::int))::int), now()
            from generate_series(1,$4::int) n""",
            GOODS,
            run_date - dt.timedelta(days=1),
            HISTORY_DAYS,
            STORED_FEEDBACKS,
        )
        await conn.execute(
            """insert into wb.daily_snapshots
            (nm_id,snapshot_date,rating,ratings_total,ratings_5,ratings_4,ratings_3,
             ratings_2,ratings_1,buyout_percent,orders_count,buyouts_count,collected_at)
            with feedback_daily as (
                select nm_id, created_date,
                       count(*)::int as total,
                       count(*) filter (where valuation=5)::int as five,
                       count(*) filter (where valuation=4)::int as four,
                       count(*) filter (where valuation=3)::int as three,
                       count(*) filter (where valuation=2)::int as two,
                       count(*) filter (where valuation=1)::int as one
                  from wb.feedbacks group by nm_id, created_date
            ), daily as (
                select p.nm_id, days.day::date as snapshot_date,
                       coalesce(fd.total,0) total, coalesce(fd.five,0) five,
                       coalesce(fd.four,0) four, coalesce(fd.three,0) three,
                       coalesce(fd.two,0) two, coalesce(fd.one,0) one
                  from wb.products p
                  cross join lateral generate_series(
                      $1::date - ($2::int - 1), $1::date, interval '1 day'
                  ) days(day)
                  left join feedback_daily fd
                    on fd.nm_id=p.nm_id and fd.created_date=days.day::date
            ), cumulative as (
                select nm_id,snapshot_date,
                       (sum(total) over w)::int as total,
                       (sum(five) over w)::int as five,
                       (sum(four) over w)::int as four,
                       (sum(three) over w)::int as three,
                       (sum(two) over w)::int as two,
                       (sum(one) over w)::int as one
                  from daily
                  window w as (partition by nm_id order by snapshot_date
                               rows between unbounded preceding and current row)
            )
            select nm_id,snapshot_date,4.50 + (nm_id % 50)::numeric / 100,
                   total,five,four,three,two,one,92.5,100,92,now()
              from cumulative""",
            run_date - dt.timedelta(days=1),
            HISTORY_DAYS,
        )
        await conn.execute(
            """insert into wb.nomenclature(account_key,vendor_code,pack,line,category,imported_at)
            select account_key,vendor_code,'1','Perf','Benchmark',now() from wb.products"""
        )
        counts = await conn.fetchrow(
            """select (select count(*) from wb.products) products,
                      (select count(*) from wb.daily_snapshots) snapshots,
                      (select count(*) from wb.feedbacks) feedbacks,
                      version() as postgres_version"""
        )
        if (
            counts["products"] != GOODS
            or counts["snapshots"] != GOODS * HISTORY_DAYS
            or counts["feedbacks"] != STORED_FEEDBACKS
        ):
            raise HarnessError(f"Seed row counts do not match benchmark plan: {dict(counts)}")
        counts = dict(counts)
        mismatches = await conn.fetchval(
            """with sample_ids(nm_id) as (values
                   (710000001::bigint),(710000500::bigint),
                   (710000501::bigint),(710001000::bigint)),
            daily as (
              select f.nm_id,f.created_date,count(*)::int total,
                     count(*) filter(where valuation=5)::int five,
                     count(*) filter(where valuation=4)::int four,
                     count(*) filter(where valuation=3)::int three,
                     count(*) filter(where valuation=2)::int two,
                     count(*) filter(where valuation=1)::int one
                from wb.feedbacks f join sample_ids s using(nm_id)
               group by f.nm_id,f.created_date),
            expected as (
              select s.nm_id,d.day::date snapshot_date,
                     sum(coalesce(x.total,0)) over w total,
                     sum(coalesce(x.five,0)) over w five,
                     sum(coalesce(x.four,0)) over w four,
                     sum(coalesce(x.three,0)) over w three,
                     sum(coalesce(x.two,0)) over w two,
                     sum(coalesce(x.one,0)) over w one
                from sample_ids s cross join lateral generate_series(
                  $1::date - ($2::int - 1), $1::date, interval '1 day') d(day)
                left join daily x on x.nm_id=s.nm_id and x.created_date=d.day::date
                window w as (partition by s.nm_id order by d.day
                             rows between unbounded preceding and current row))
            select count(*) from expected e join wb.daily_snapshots s
              using(nm_id,snapshot_date)
             where (e.total,e.five,e.four,e.three,e.two,e.one) is distinct from
                   (s.ratings_total,s.ratings_5,s.ratings_4,s.ratings_3,s.ratings_2,s.ratings_1)""",
            run_date - dt.timedelta(days=1),
            HISTORY_DAYS,
        )
        if mismatches:
            raise HarnessError(
                f"Seed snapshot history differs from feedback history for {mismatches} sampled rows"
            )
        counts["sampled_snapshot_rows_verified"] = 4 * HISTORY_DAYS
        return counts
    finally:
        await conn.close()


def _validate_database_target(database_url: str, env: dict[str, str]) -> None:
    expected_name = env.get("WB_E2E_DATABASE_NAME", "")
    expected_url = env.get("WB_E2E_DATABASE_URL", "")
    parts = urlsplit(database_url)
    if (
        not PERF_DB_PATTERN.fullmatch(expected_name)
        or database_url != expected_url
        or parts.path != f"/{expected_name}"
        or parts.hostname not in {"127.0.0.1", "localhost"}
        or parts.query
        or parts.fragment
    ):
        raise HarnessError("Seed refused a database outside this run's isolated loopback database")


def _child_env(database_url: str, database_name: str) -> dict[str, str]:
    e2e = _e2e_runner()
    env = e2e._child_env(database_url, blank_integrations=True)
    env["WB_E2E_DATABASE_NAME"] = database_name
    env["WB_E2E_ADMIN_PASSWORD"] = secrets.token_urlsafe(24)
    env["WB_E2E_VIEWER_PASSWORD"] = secrets.token_urlsafe(24)
    for i in range(1, 6):
        env[f"WB_PERF_USER_{i}_PASSWORD"] = secrets.token_urlsafe(24)
    env["WB_E2E_PERFORMANCE"] = "1"
    return env


def _terminate(child: subprocess.Popen[str]) -> None:
    e2e = _e2e_runner()
    e2e._terminate(child)
    ACTIVE_CHILDREN.discard(child)


def _drop_owned_database(admin_url: str, database_name: str) -> None:
    """Drop one guarded benchmark DB in a clean loop, outside Playwright's loop."""
    validate_admin_url(admin_url)
    if not PERF_DB_PATTERN.fullmatch(database_name):
        raise HarnessError("Refusing cleanup for a database outside the generated-name guard")
    cleanup_env = os.environ.copy()
    cleanup_env["WB_E2E_ADMIN_DATABASE_URL"] = admin_url
    code = (
        "import sys; sys.path.insert(0, 'scripts'); import run_e2e; "
        "run_e2e._drop_database(run_e2e._load_admin_url(), sys.argv[1])"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code, database_name],
        cwd=ROOT,
        env=cleanup_env,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        raise HarnessError("Could not remove the owned benchmark database")


def _launch_server(
    env: dict[str, str], output_dir: Path
) -> tuple[subprocess.Popen[str], socket.socket, str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(socket.SOMAXCONN)
    listener.set_inheritable(True)
    base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    env["WB_E2E_BASE_URL"] = base_url
    env["WB_E2E_SOCKET_FD"] = str(listener.fileno())
    log = (output_dir / "server.log").open("w", encoding="utf-8")
    child = subprocess.Popen(
        [sys.executable, "-m", "e2e.server"],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        pass_fds=(listener.fileno(),),
        start_new_session=True,
    )
    ACTIVE_CHILDREN.add(child)
    return child, listener, base_url


def _seed_users(env: dict[str, str]) -> None:
    # e2e.seed inserts its own small fixture set, so only use its user creation
    # contract via a narrowly scoped SQL command to keep the benchmark dataset exact.
    code = """import asyncio, os
from app.config import load_settings
from app.db.engine import build_engine
from app.db.session import build_session_factory
from app.services.auth import create_user
from e2e.runtime import database_url
async def main():
 e=build_engine(database_url())
 try:
  async with build_session_factory(e)() as s:
   await create_user(s, username='e2eadmin', REDACTED=os.environ['WB_E2E_ADMIN_PASSWORD'],
                     role='admin', policy=load_settings().auth)
   await create_user(s, username='e2eviewer', REDACTED=os.environ['WB_E2E_VIEWER_PASSWORD'],
                     role='viewer', policy=load_settings().auth)
   for i in range(1, 6):
    await create_user(s, username=f'perfuser{i}',
                      REDACTED=os.environ[f'WB_PERF_USER_{i}_PASSWORD'],
                      role='admin' if i == 1 else 'viewer', policy=load_settings().auth)
   await s.commit()
 finally: await e.dispose()
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True
    )
    if result.returncode:
        raise HarnessError("Could not create isolated browser test users")


def _browser_benchmark(base_url: str, env: dict[str, str], output_dir: Path) -> dict[str, object]:
    from playwright.sync_api import sync_playwright

    pw_started = time.perf_counter()
    metrics: dict[str, object] = {
        "first_interactive_session_seconds": None,
        "cold_fresh_browser_session_seconds": None,
        "serial_seconds": [],
        "parallel_seconds": [],
        "card_serial_seconds": [],
        "card_parallel_seconds": [],
    }
    serial: list[float] = []
    progress_path = output_dir / "web-progress.json"

    def checkpoint() -> None:
        progress_path.write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    base = urlsplit(base_url)
    allowed_origin = f"{base.scheme}://{base.netloc}"

    def allowed_url(url: str) -> bool:
        parts = urlsplit(url)
        return (
            parts.scheme in {"data", "blob"} or f"{parts.scheme}://{parts.netloc}" == allowed_origin
        )

    def is_ratings_response(response, start_date: str, end_date: str) -> bool:
        parts = urlsplit(response.url)
        from urllib.parse import parse_qs

        query = parse_qs(parts.query)
        return (
            parts.path == "/api/reports/ratings"
            and query.get("start") == [start_date]
            and query.get("end") == [end_date]
        )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)

        def measure(username: str, REDACTED: str):
            context = browser.new_context()
            page = context.new_page()
            page.route(
                "**/*",
                lambda route: (
                    route.continue_() if allowed_url(route.request.url) else route.abort()
                ),
            )
            cold_started = time.perf_counter()
            page.goto(base_url, wait_until="domcontentloaded")
            page.get_by_label("Логин").fill(username)
            page.get_by_label("Пароль").fill(REDACTED)
            with page.expect_response(
                lambda response: urlsplit(response.url).path == "/api/login"
            ) as login_info:
                page.get_by_role("button", name="Войти").click()
            login_response = login_info.value
            if login_response.status != 200:
                raise HarnessError("Isolated browser login failed")
            token = login_response.json()["access_token"]
            page.get_by_role("navigation", name="Разделы").wait_for()
            page.get_by_role("button", name="Все артикулы").click()
            page.locator("table tbody tr").first.wait_for(timeout=120_000)
            cold_elapsed = time.perf_counter() - cold_started
            end_date = dt.date.fromisoformat(env["WB_PERF_RUN_DATE"])
            start_date = (end_date - dt.timedelta(days=89)).isoformat()
            end_value = end_date.isoformat()
            page.locator("#end-date").fill(end_value)
            page.locator("#start-date").fill(start_date)
            # The measurement begins at the period-apply action and ends only
            # after the table's ready state and its rendered rows are present.
            period_button = page.locator("#apply-period")
            start = time.perf_counter()
            with page.expect_response(
                lambda response: is_ratings_response(response, start_date, end_value),
                timeout=120_000,
            ) as report_info:
                period_button.click(timeout=20_000)
            report_response = report_info.value
            report_payload = report_response.json()
            if report_response.status != 200 or not _has_complete_boundaries(report_payload):
                raise HarnessError(
                    "90-day report needs 1000 products with current and previous boundaries"
                )
            page.wait_for_function(
                """() => new Promise(resolve => requestAnimationFrame(() =>
                    requestAnimationFrame(() => resolve(
                      document.querySelectorAll('#content table tbody tr').length === 1000))))""",
                timeout=120_000,
            )
            if page.locator("#content table tbody tr").count() != GOODS:
                raise HarnessError("90-day browser report did not render all 1000 products")
            period_note = page.locator(".period-note").inner_text()
            start_text = dt.date.fromisoformat(start_date).strftime("%d.%m.%Y")
            end_text = end_date.strftime("%d.%m.%Y")
            if (
                (end_date - dt.date.fromisoformat(page.locator("#start-date").input_value())).days
                != 89
                or start_text not in period_note
                or end_text not in period_note
            ):
                raise HarnessError("Browser benchmark period is not 90 days")
            elapsed = time.perf_counter() - start
            context.close()
            return elapsed, cold_elapsed, token

        def measure_card(username: str, REDACTED: str) -> float:
            context = browser.new_context()
            page = context.new_page()
            page.route(
                "**/*",
                lambda route: (
                    route.continue_() if allowed_url(route.request.url) else route.abort()
                ),
            )
            page.goto(base_url, wait_until="domcontentloaded")
            page.get_by_label("Логин").fill(username)
            page.get_by_label("Пароль").fill(REDACTED)
            with page.expect_response(
                lambda response: urlsplit(response.url).path == "/api/login"
            ) as login_info:
                page.get_by_role("button", name="Войти").click()
            if login_info.value.status != 200:
                raise HarnessError("Isolated product-card login failed")
            page.get_by_role("navigation", name="Разделы").wait_for()
            page.get_by_role("button", name="Все артикулы").click()
            page.locator("#content table tbody tr").first.wait_for(timeout=120_000)
            page.locator('[data-product="710000001"]').wait_for()
            page.locator('[data-product="710000001"]').click()
            page.locator("#content .product-comparison").wait_for(timeout=120_000)
            end_date = dt.date.fromisoformat(env["WB_PERF_RUN_DATE"])
            start_date = end_date - dt.timedelta(days=89)
            start_text, end_text = start_date.strftime("%d.%m.%Y"), end_date.strftime("%d.%m.%Y")
            page.locator("#start-date").fill(start_date.isoformat())
            page.locator("#end-date").fill(end_date.isoformat())
            started = time.perf_counter()
            with (
                page.expect_response(
                    lambda response: (
                        urlsplit(response.url).path == "/api/products/710000001/snapshots"
                        and f"start={start_date.isoformat()}" in response.url
                        and f"end={end_date.isoformat()}" in response.url
                    ),
                    timeout=120_000,
                ) as snapshot_info,
                page.expect_response(
                    lambda response: (
                        is_ratings_response(response, start_date.isoformat(), end_date.isoformat())
                        and "nm_id=710000001" in response.url
                    ),
                    timeout=120_000,
                ) as ratings_info,
            ):
                page.locator("#apply-period").click(timeout=20_000)
            if (
                snapshot_info.value.status != 200
                or ratings_info.value.status != 200
                or not _has_complete_boundaries(ratings_info.value.json(), expected_rows=1)
            ):
                raise HarnessError("90-day product snapshot response failed")
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
            elapsed = time.perf_counter() - started
            context.close()
            return elapsed

        # Record initial UI latency after the separately reported SQL warmup.
        _, cold_elapsed, token = measure("e2eadmin", env["WB_E2E_ADMIN_PASSWORD"])
        metrics["first_interactive_session_seconds"] = cold_elapsed
        metrics["cold_fresh_browser_session_seconds"] = cold_elapsed
        checkpoint()
        api = playwright.request.new_context(
            extra_http_headers={"Authorization": f"Bearer {token}"}
        )
        end_date = dt.date.fromisoformat(env["WB_PERF_RUN_DATE"])
        start_date = end_date - dt.timedelta(days=89)
        api_values: list[float] = []
        for index in range(30):
            started = time.perf_counter()
            response = api.get(
                f"{base_url}/api/reports/ratings",
                params={"start": start_date.isoformat(), "end": end_date.isoformat()},
                timeout=120_000,
            )
            payload = response.json()
            elapsed = time.perf_counter() - started
            if response.status != 200 or not _has_complete_boundaries(payload):
                raise HarnessError(
                    "Authenticated report needs 1000 products with current and previous boundaries"
                )
            api_values.append(elapsed)
            if (index + 1) % 10 == 0:
                _log(f"authenticated API measurements: {index + 1}/30")
        api.dispose()
        metrics["api_seconds"] = api_values
        metrics["first_90_day_api_seconds_after_sql_warmup"] = api_values[0]
        metrics["api_p95_seconds"] = percentile(api_values, 95)
        checkpoint()
        for _ in range(30):
            interactive, _, _ = measure("e2eviewer", env["WB_E2E_VIEWER_PASSWORD"])
            serial.append(interactive)
            if len(serial) % 5 == 0:
                _log(f"browser serial measurements: {len(serial)}/30")
        metrics["serial_seconds"] = serial
        metrics["serial_p95_seconds"] = percentile(serial, 95)
        metrics["serial_target_met"] = metrics["serial_p95_seconds"] <= 2.0
        checkpoint()
        card_serial: list[float] = []
        for _ in range(30):
            card_serial.append(measure_card("e2eviewer", env["WB_E2E_VIEWER_PASSWORD"]))
            if len(card_serial) % 5 == 0:
                _log(f"product-card serial measurements: {len(card_serial)}/30")
        metrics["card_serial_seconds"] = card_serial
        metrics["card_serial_p95_seconds"] = percentile(card_serial, 95)
        metrics["card_serial_target_met"] = metrics["card_serial_p95_seconds"] <= 2.0
        checkpoint()
        state_dir = output_dir / "parallel-browser"
        state_dir.mkdir(exist_ok=True)
        workers: list[subprocess.Popen[str]] = []
        # Each real user initializes an independent browser/context and waits at
        # the same barrier before each simultaneous measurement round.
        for user_id in range(1, 6):
            username = f"perfuser{user_id}"
            log_path = output_dir / f"parallel-user-{user_id}.log"
            log_file = log_path.open("w", encoding="utf-8")
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "performance.browser_worker",
                    base_url,
                    username,
                    str(state_dir),
                    "--cards",
                ],
                cwd=ROOT,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            workers.append(child)
        ACTIVE_CHILDREN.update(workers)

        def wait_for_files(paths: list[Path], timeout: float) -> None:
            deadline = time.monotonic() + timeout
            while not all(path.exists() for path in paths):
                if any(child.poll() is not None and child.returncode for child in workers):
                    raise HarnessError("Parallel browser worker exited; inspect parallel-user logs")
                if time.monotonic() >= deadline:
                    raise HarnessError("Parallel browser barrier timed out")
                time.sleep(0.05)

        wait_for_files([state_dir / f"ready-{uid}" for uid in range(1, 6)], 180)
        parallel: list[float] = []
        for round_id in range(1, 7):
            (state_dir / f"go-{round_id}").write_text("go", encoding="utf-8")
            done_files = [state_dir / f"done-{uid}-{round_id}" for uid in range(1, 6)]
            wait_for_files(done_files, 180)
            parallel.extend(
                float(json.loads(path.read_text(encoding="utf-8"))["seconds"])
                for path in done_files
            )
            _log(f"parallel browser measurements: {len(parallel)}/30")
        metrics["parallel_seconds"] = parallel
        metrics["parallel_p95_seconds"] = percentile(parallel, 95)
        metrics["parallel_target_met"] = metrics["parallel_p95_seconds"] <= 2.0
        metrics["parallel_users"] = 5
        checkpoint()
        card_parallel: list[float] = []
        for round_id in range(1, 7):
            (state_dir / f"card-go-{round_id}").write_text("go", encoding="utf-8")
            done_files = [state_dir / f"card-done-{uid}-{round_id}" for uid in range(1, 6)]
            wait_for_files(done_files, 180)
            card_parallel.extend(
                float(json.loads(path.read_text(encoding="utf-8"))["seconds"])
                for path in done_files
            )
            _log(f"parallel product-card measurements: {len(card_parallel)}/30")
        for child in workers:
            if child.poll() is None:
                child.wait(timeout=30)
            ACTIVE_CHILDREN.discard(child)
            if child.returncode:
                raise HarnessError(
                    "Parallel product-card browser worker failed; inspect parallel-user logs"
                )
        metrics["card_parallel_seconds"] = card_parallel
        metrics["card_parallel_p95_seconds"] = percentile(card_parallel, 95)
        metrics["card_parallel_target_met"] = metrics["card_parallel_p95_seconds"] <= 2.0
        checkpoint()
        metrics["browser_version"] = browser.version
        browser.close()
    metrics["elapsed_seconds"] = time.perf_counter() - pw_started
    checkpoint()
    return metrics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--web-only", action="store_true", help="seed then benchmark only the browser/API"
    )
    args = parser.parse_args(argv)
    run_id = uuid.uuid4().hex
    # e2e.server and e2e.runtime both enforce this exact disposable-name
    # contract before opening the database.
    db_name = f"wb_e2e_{run_id}"
    output_dir = ROOT / "outputs" / "performance" / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}_{run_id}"
    output_dir.mkdir(parents=True, exist_ok=False)
    result: dict[str, object] = {
        "run_id": run_id,
        "status": "running",
        "database_name": db_name,
        "started_at": datetime.now(UTC).isoformat(),
        "system": _system_info(),
        "targets": {"collection_seconds": TARGET_SECONDS, "browser_serial_p95_seconds": 2.0},
        "dataset": {
            "goods": GOODS,
            "accounts": 2,
            "snapshots_per_good": HISTORY_DAYS,
            "stored_feedbacks": STORED_FEEDBACKS,
            "new_feedbacks": NEW_FEEDBACKS,
        },
        "collection": None,
        "web": None,
        "failure": None,
        "database_created": False,
        "database_removed": False,
    }
    result_path = output_dir / "results.json"

    def save() -> None:
        result_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
        )

    save()
    e2e = _e2e_runner()
    admin_url = os.environ.get("WB_E2E_ADMIN_DATABASE_URL", "").strip() or e2e._load_admin_url()
    db_created = False
    server: subprocess.Popen[str] | None = None
    listener: socket.socket | None = None
    env: dict[str, str] = {}
    failure: Exception | None = None
    signal.signal(signal.SIGTERM, e2e._sigterm_handler)
    try:
        validate_admin_url(admin_url)
        if not PERF_DB_PATTERN.fullmatch(db_name):
            raise HarnessError("Generated benchmark database name failed safety validation")
        db_created = e2e._create_database(admin_url, db_name)
        if not db_created:
            raise HarnessError("Generated database already exists; refusing to use it")
        result["database_created"] = True
        db_url = e2e._database_url(admin_url, db_name)
        env = _child_env(db_url, db_name)
        migration = subprocess.run(
            [sys.executable, "-m", "app.migrate"], cwd=ROOT, env=env, capture_output=True, text=True
        )
        (output_dir / "migration.log").write_text(
            migration.stdout + migration.stderr, encoding="utf-8"
        )
        if migration.returncode:
            raise HarnessError("Migration failed; inspect migration.log")
        _seed_users(env)
        run_date = dt.datetime.now(UTC).astimezone(ZoneInfo("Europe/Moscow")).date() - dt.timedelta(
            days=1
        )
        started_seed = time.perf_counter()
        result["seed_counts"] = asyncio.run(_seed_database(db_url, run_date, env))
        result["seed_seconds"] = time.perf_counter() - started_seed
        save()
        # Seed snapshots end the day before run_date. Collection runs can
        # populate run_date; web-only runs must use the seeded final boundary.
        web_run_date = run_date if not args.web_only else run_date - dt.timedelta(days=1)
        env["WB_PERF_RUN_DATE"] = web_run_date.isoformat()
        e2e.REDACT_VALUES.extend(env[key] for key in env if key.endswith("_PASSWORD") and env[key])
        _log("seed complete; starting isolated local API")
        server_started = time.perf_counter()
        server, listener, base_url = _launch_server(env, output_dir)
        listener.close()
        listener = None
        e2e._wait_for_server(server, base_url, timeout_seconds=45)
        result["server_start_seconds"] = time.perf_counter() - server_started
        if not args.web_only:
            collect_started = time.perf_counter()
            collect_log = output_dir / "collect.log"
            with collect_log.open("w", encoding="utf-8") as log_file:
                collected = subprocess.Popen(
                    [sys.executable, "-m", "performance.collect_run", run_date.isoformat()],
                    cwd=ROOT,
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
                ACTIVE_CHILDREN.add(collected)
                last_progress = time.monotonic()
                while collected.poll() is None:
                    time.sleep(1)
                    if time.monotonic() - last_progress >= 25:
                        _log(f"collection running for {time.perf_counter() - collect_started:.0f}s")
                        last_progress = time.monotonic()
                ACTIVE_CHILDREN.discard(collected)
            collect_output = collect_log.read_text(encoding="utf-8", errors="replace")
            result["collection"] = {
                "elapsed_seconds": time.perf_counter() - collect_started,
                "exit_code": collected.returncode,
                "summary": _parse_last_json(collect_output),
            }
            save()
            if collected.returncode:
                raise HarnessError("Collector benchmark failed; inspect collect.log")
            actual = result["collection"]
            assert isinstance(actual, dict)
            if actual["elapsed_seconds"] > TARGET_SECONDS:
                raise HarnessError("Full collection exceeded 7200-second target")
        period_end = web_run_date
        period_start = period_end - dt.timedelta(days=89)
        from performance.sql_benchmark import run as run_sql_benchmark

        result["sql"] = run_sql_benchmark(db_url, period_start, period_end)
        save()
        result["web"] = _browser_benchmark(base_url, env, output_dir)
        save()
        web_result = result["web"]
        if isinstance(web_result, dict):
            if not web_result.get("serial_target_met"):
                raise HarnessError("Browser serial p95 exceeded the 2-second target")
            if not web_result.get("parallel_target_met"):
                raise HarnessError("Browser parallel p95 exceeded the 2-second target")
            if not web_result.get("card_serial_target_met"):
                raise HarnessError("Product-card serial p95 exceeded the 2-second target")
            if not web_result.get("card_parallel_target_met"):
                raise HarnessError("Product-card parallel p95 exceeded the 2-second target")
        result["status"] = "passed"
    except KeyboardInterrupt:
        result["status"] = "interrupted"
        result["failure"] = "Runner interrupted"
        failure = HarnessError("Runner interrupted")
    except Exception as exc:
        result["status"] = "failed"
        result["failure"] = (
            str(exc)
            if isinstance(exc, HarnessError)
            else f"Failed during {type(exc).__name__}; inspect run logs"
        )
        failure = exc
        (output_dir / "runner.log").write_text(traceback.format_exc(), encoding="utf-8")
        progress_path = output_dir / "web-progress.json"
        if progress_path.exists():
            with suppress(OSError, json.JSONDecodeError):
                result["web"] = json.loads(progress_path.read_text(encoding="utf-8"))
    finally:
        for child in tuple(ACTIVE_CHILDREN):
            with suppress(Exception):
                _terminate(child)
        if listener is not None:
            listener.close()
        if server is not None and server.poll() is None:
            with suppress(Exception):
                _terminate(server)
        if db_created:
            try:
                _drop_owned_database(admin_url, db_name)
                result["database_removed"] = True
            except Exception:
                result["cleanup_succeeded"] = False
                result["failure"] = (
                    result.get("failure") or "Could not remove the owned benchmark database"
                )
                result["status"] = "failed"
        result["server_stopped"] = server is None or server.poll() is not None
        result["cleanup_succeeded"] = bool(result.get("database_removed")) and bool(
            result["server_stopped"]
        )
        result["finished_at"] = datetime.now(UTC).isoformat()
        for log_path in output_dir.glob("*.log"):
            with suppress(OSError):
                log_path.write_text(
                    e2e._redact(log_path.read_text(encoding="utf-8", errors="replace")),
                    encoding="utf-8",
                )
        save()
    _log(f"performance {result['status']}; artifacts: {output_dir}")
    return (
        130
        if result["status"] == "interrupted"
        else (1 if failure or result["status"] != "passed" else 0)
    )


def _parse_last_json(text: str) -> object:
    for line in reversed(text.splitlines()):
        if line.startswith("PERF_RESULT="):
            try:
                return json.loads(line.removeprefix("PERF_RESULT="))
            except json.JSONDecodeError as exc:
                raise HarnessError("Collector summary is not valid JSON") from exc
    raise HarnessError("Collector summary absent")


if __name__ == "__main__":
    raise SystemExit(main())
