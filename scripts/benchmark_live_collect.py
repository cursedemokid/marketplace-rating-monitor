"""Measure a read-only WB collection after an explicitly supplied local backup."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import platform
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from app import jobs
from app.adapters.wb.client import WbClient
from app.clock import SystemClock
from app.config import load_secrets, load_settings
from app.db.engine import build_engine
from app.db.session import build_session_factory
from loguru import logger
from sqlalchemy import text


async def run(backup: Path) -> int:
    if not backup.is_file() or backup.stat().st_size == 0:
        raise ValueError("A completed PostgreSQL backup is required")
    settings, secrets = load_settings(), load_secrets()
    clock = SystemClock(settings.period.tz)
    day = clock.today() - dt.timedelta(days=1)
    output = Path("outputs/performance_live") / clock.now().strftime("%Y%m%dT%H%M%S")
    output.mkdir(parents=True, exist_ok=False)
    logger.remove()
    logger.add(output / "collect.log", level="INFO")
    logger.add(lambda message: print(message, end="", flush=True), level="WARNING")
    requests: Counter[str] = Counter()
    responses: Counter[str] = Counter()
    logical: Counter[str] = Counter()
    duration: Counter[str] = Counter()
    accounts = {p.feedbacks_token: p.key for p in jobs.resolve_wb_profiles(secrets)}
    original_init, original_request = WbClient.__init__, WbClient._request

    def instrument(client: WbClient, token: str, **kwargs: object) -> None:
        original_init(client, token, **kwargs)
        account = accounts[token]

        async def on_request(request: httpx.Request) -> None:
            key = f"{account}:{request.method}:{request.url.path}"
            requests[key] += 1
            request.extensions["benchmark_start"] = time.perf_counter()

        async def on_response(response: httpx.Response) -> None:
            request = response.request
            key = f"{account}:{request.method}:{request.url.path}"
            responses[f"{key}:{response.status_code}"] += 1
            duration[key] += time.perf_counter() - request.extensions["benchmark_start"]

        client._client.event_hooks = {"request": [on_request], "response": [on_response]}

    async def counted(client: WbClient, method: str, url: str, *args: object, **kw: object):
        logical[f"{method}:{urlsplit(url).path}"] += 1
        return await original_request(client, method, url, *args, **kw)

    engine = build_engine(secrets.database_url)
    factory = build_session_factory(engine)

    async def state() -> dict[str, object]:
        async with factory() as session:
            rows = await session.execute(
                text("""
                select account_key,count(*) as products
                from wb.products where is_monitored group by account_key
                """)
            )
            result: dict[str, object] = {"products": [dict(row._mapping) for row in rows]}
            for table in ("signals", "ai_cost_requests", "feedbacks"):
                value = await session.scalar(text(f"select count(*) from wb.{table}"))
                result[table] = value
            return result

    result: dict[str, object] = {
        "kind": "real_wb_current_cabinets",
        "run_date": day.isoformat(),
        "started_at": clock.now().isoformat(),
        "backup": str(backup),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "recheck_days": settings.collector.recheck_days,
        "real_1000_products_verified": False,
        "status": "running",
    }
    path = output / "result.json"
    code = 1
    start = time.perf_counter()
    try:
        result["before"] = await state()
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        WbClient.__init__, WbClient._request = instrument, counted
        async with jobs.manual_run_lock(secrets), asyncio.timeout(7200):
            collection = await jobs.collect(settings, secrets, clock, day)
        result["collection"] = asdict(collection)
        result["after"] = await state()
        result["status"] = (
            "passed"
            if not (collection.errors or collection.skipped_feedbacks or collection.failed_products)
            else "partial"
        )
        code = 0 if result["status"] == "passed" else 1
    except BaseException as exc:
        # Error type only: provider exception text may contain sensitive data.
        result["status"], result["error_type"] = "failed", type(exc).__name__
    finally:
        WbClient.__init__, WbClient._request = original_init, original_request
        result.update(
            elapsed_seconds=time.perf_counter() - start,
            finished_at=clock.now().isoformat(),
            http_attempts=dict(requests),
            http_responses=dict(responses),
            logical_calls=dict(logical),
            retry_attempts=max(0, sum(requests.values()) - sum(logical.values())),
            http_seconds=dict(duration),
        )
        await engine.dispose()
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(f"Real WB benchmark: {result['status']}; {path}", flush=True)
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, required=True)
    arguments = parser.parse_args()
    raise SystemExit(asyncio.run(run(arguments.backup)))
