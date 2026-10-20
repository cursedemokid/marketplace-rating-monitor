"""Time the production 90-day ratings report at SQLAlchemy's DB boundary."""

from __future__ import annotations

import asyncio
import datetime as dt
import time

from app.db.engine import build_engine
from app.db.session import build_session_factory
from app.repositories.product_filters import ProductFilters, select_product_ids
from app.repositories.reporting import load_rating_report
from sqlalchemy import event


async def benchmark(
    database_url: str,
    start: dt.date,
    end: dt.date,
    *,
    repetitions: int = 30,
    warmups: int = 3,
) -> dict[str, object]:
    engine = build_engine(database_url)
    statement_times: list[float] = []

    def before(conn, _cursor, _statement, _parameters, _context, _many) -> None:
        conn.info.setdefault("performance_sql_started", []).append(time.perf_counter())

    def after(conn, _cursor, _statement, _parameters, _context, _many) -> None:
        started = conn.info["performance_sql_started"].pop()
        statement_times.append(time.perf_counter() - started)

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    event.listen(engine.sync_engine, "after_cursor_execute", after)
    report_times: list[float] = []
    factory = build_session_factory(engine)
    try:
        # Prime PostgreSQL's plan and data caches separately from timed samples.
        for _ in range(warmups):
            async with factory() as session:
                ids = await select_product_ids(session, ProductFilters())
                report = await load_rating_report(session, start=start, end=end, nm_ids=ids)
                if len(report.rows) != 1000:
                    raise RuntimeError(
                        f"SQL report returned {len(report.rows)} rows, expected 1000"
                    )
        # The SQLAlchemy event listener measured warmups too; exclude them from
        # the timed statement count and latency distribution below.
        statement_times.clear()
        for _ in range(repetitions):
            started = time.perf_counter()
            async with factory() as session:
                ids = await select_product_ids(session, ProductFilters())
                report = await load_rating_report(session, start=start, end=end, nm_ids=ids)
                if len(report.rows) != 1000:
                    raise RuntimeError(
                        f"SQL report returned {len(report.rows)} rows, expected 1000"
                    )
            report_times.append(time.perf_counter() - started)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before)
        event.remove(engine.sync_engine, "after_cursor_execute", after)
        await engine.dispose()

    from performance.harness import percentile

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "warmup_repetitions_excluded": warmups,
        "repetitions": repetitions,
        "report_wall_seconds": report_times,
        "report_wall_p95_seconds": percentile(report_times, 95),
        "sql_statement_count": len(statement_times),
        "sql_statement_seconds": statement_times,
        "sql_statement_p95_seconds": percentile(statement_times, 95),
        "sql_statement_total_seconds": sum(statement_times),
    }


def run(
    database_url: str, start: dt.date, end: dt.date, *, repetitions: int = 30
) -> dict[str, object]:
    return asyncio.run(benchmark(database_url, start, end, repetitions=repetitions))
