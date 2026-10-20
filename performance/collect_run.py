"""Run the production collector against a delayed, schema-valid fake WB API."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
import time
from collections import Counter
from zoneinfo import ZoneInfo

import httpx
from app.adapters.wb.client import RateLimits, WbClient
from app.clock import ManualClock
from app.config import load_settings
from app.db.engine import build_engine
from app.db.session import build_session_factory
from app.services.collect import run
from e2e.runtime import database_url
from sqlalchemy import text

MOSCOW = ZoneInfo("Europe/Moscow")


class FakeWb:
    def __init__(self, run_date: dt.date) -> None:
        self.run_date = run_date
        self.counts: Counter[str] = Counter()
        self.status_counts: Counter[str] = Counter()
        self.errors: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        path = request.url.path
        category = (
            "content"
            if "/content/" in path
            else "analytics"
            if "/analytics/" in path
            else "feedbacks"
        )
        self.counts[category] += 1
        try:
            if path.endswith("/content/v2/get/cards/list"):
                body = json.loads(request.content)
                cursor = body["settings"]["cursor"]
                cursor_id = int(cursor.get("nmID", 0))
                # IDs are 1..500 and 501..1000. Each cabinet returns 500.
                first = 1 if self._account(request) == "default" else 501
                start = (cursor_id - 710000000 + 1) if cursor_id else first
                boundary = 501 if first == 1 else 1001
                batch = list(range(start, min(start + 100, boundary)))
                cards = [
                    {
                        "nmID": 710000000 + n,
                        "vendorCode": f"PERF-{n}",
                        "brand": "Perf",
                        "title": f"Benchmark product {n}",
                        "subjectName": "Benchmark",
                        "updatedAt": "2026-10-19T00:00:00Z",
                    }
                    for n in batch
                ]
                last = batch[-1] if batch else start - 1
                updated = (
                    dt.datetime(2026, 10, 19, tzinfo=dt.UTC) + dt.timedelta(seconds=last)
                ).isoformat()
                return self.response(
                    200,
                    {
                        "cards": cards,
                        "cursor": {
                            "updatedAt": updated,
                            "nmID": 710000000 + last,
                            "total": len(cards),
                        },
                    },
                )
            if path.endswith("/content/v2/get/cards/trash"):
                return self.response(200, {"cards": [], "cursor": {}})
            if path.endswith("/api/v1/feedbacks"):
                is_answered = request.url.params.get("isAnswered") == "true"
                skip = int(request.url.params.get("skip", 0))
                if is_answered:
                    rows = []
                else:
                    # Two accounts × 15k; cabinet range is derived from token.
                    first = (
                        1 if request.headers.get("Authorization") == "perf-token-default" else 501
                    )
                    stop = min(skip + 5000, 15000)
                    rows = [self.feedback(first, i) for i in range(skip, stop)]
                return self.response(
                    200, {"data": {"feedbacks": rows}, "error": False, "errorText": ""}
                )
            if path.endswith("/api/v1/feedbacks/archive"):
                return self.response(200, {"data": {"feedbacks": []}, "error": False})
            if path.endswith("/api/analytics/v2/item-rating"):
                body = json.loads(request.content)
                return self.response(
                    200,
                    {
                        "data": {
                            "items": [
                                {
                                    "nmId": nm_id,
                                    "feedbackRating": {"current": 4.7, "dynamics": 0},
                                    "feedbackCount": {"current": 500, "dynamics": 0},
                                }
                                for nm_id in body["nmIds"]
                            ]
                        }
                    },
                )
            if path.endswith("/api/analytics/v3/sales-funnel/products"):
                body = json.loads(request.content)
                return self.response(
                    200,
                    {
                        "data": {
                            "products": [
                                {
                                    "product": {"nmId": nm_id},
                                    "statistic": {
                                        "selected": {"orderCount": 100, "buyoutCount": 92}
                                    },
                                }
                                for nm_id in body["nmIds"]
                            ]
                        }
                    },
                )
            self.errors.append(f"unexpected {request.method} {path}")
            return self.response(404, {"detail": "unexpected fake endpoint"})
        except Exception as exc:
            self.errors.append(f"{path}: {type(exc).__name__}: {exc}")
            return self.response(500, {"detail": "fake response generation failed"})

    def response(self, status: int, payload: dict[str, object]) -> httpx.Response:
        self.status_counts[str(status)] += 1
        return httpx.Response(status, json=payload)

    @staticmethod
    def _account(request: httpx.Request) -> str:
        return (
            "default" if request.headers.get("Authorization") == "perf-token-default" else "DemoSeller"
        )

    def feedback(self, account_first: int, index: int) -> dict[str, object]:
        nm_id = 710000000 + account_first + (index % 500)
        created = self.run_date - dt.timedelta(days=index % 7)
        return {
            "id": f"perf-new-{account_first}-{index}",
            "text": f"New feedback {index}",
            "pros": "",
            "cons": "",
            "productValuation": 2 if index % 10 == 0 else 5,
            "createdDate": dt.datetime.combine(created, dt.time(12), dt.UTC).isoformat(),
            "productDetails": {"nmId": nm_id},
        }


async def main(run_date: dt.date) -> None:
    # Two actual WbClient instances preserve separate account auth headers and
    # production default RateLimits. Only their httpx transports are replaced.
    fake = FakeWb(run_date)
    settings = load_settings()
    recheck_days = settings.collector.recheck_days
    engine = build_engine(database_url())
    results: list[dict[str, object]] = []
    started = time.perf_counter()
    try:
        async with build_session_factory(engine)() as session:
            for account, token in (
                ("default", "perf-token-default"),
                ("DemoSeller", "perf-token-DemoSeller"),
            ):
                async with WbClient(
                    token, content_token=token, analytics_token=token, limits=RateLimits()
                ) as client:
                    await client._client.aclose()
                    client._client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
                    clock = ManualClock(dt.datetime.combine(run_date, dt.time(9), MOSCOW), MOSCOW)
                    result = await run(
                        session,
                        client,
                        clock,
                        run_date=run_date,
                        recheck_days=recheck_days,
                        account_key=account,
                    )
                    results.append(
                        {
                            "account": account,
                            "products": result.products,
                            "new_feedbacks_saved": result.feedbacks,
                            "skipped_feedbacks": result.skipped_feedbacks,
                            "snapshots_written": result.snapshots,
                            "rechecked_dates": result.rechecked_dates,
                            "failed_products": result.failed_products,
                            "errors": list(result.errors),
                        }
                    )
                    if result.errors or result.failed_products or result.feedbacks != 15_000:
                        raise RuntimeError(
                            f"collection result not complete for {account}: {result}"
                        )
        async with engine.connect() as conn:
            counts = (
                (
                    await conn.execute(
                        text("""select
                (select count(*) from wb.products) products,
                (select count(*) from wb.daily_snapshots) snapshots,
                (select count(*) from wb.feedbacks) feedbacks,
                (select count(distinct id) from wb.feedbacks
                   where id like 'perf-new-%') new_distinct,
                (select count(*) from wb.collection_runs where status='ok') successful_runs""")
                    )
                )
                .mappings()
                .one()
            )
        payload = {
            "elapsed_seconds": time.perf_counter() - started,
            "recheck_days": recheck_days,
            "accounts": results,
            "http_requests_by_category": dict(fake.counts),
            "http_request_count": sum(fake.counts.values()),
            "http_status_counts": dict(fake.status_counts),
            "http_delay_seconds": 0.2,
            "fake_http_errors": fake.errors,
            "database_counts": dict(counts),
        }
        if counts["new_distinct"] != 30_000 or counts["feedbacks"] != 530_000 or len(results) != 2:
            raise RuntimeError(f"post-collection counts failed acceptance check: {counts}")
        print("PERF_RESULT=" + json.dumps(payload, default=str), flush=True)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m performance.collect_run YYYY-MM-DD")
    asyncio.run(main(dt.date.fromisoformat(sys.argv[1])))
