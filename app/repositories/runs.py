"""Журнал прогонов сбора. FR-1.4, TSK-107.

Без журнала непонятно, отработал ли ночной сбор и сколько артикулов упало,
а это первое, что спрашивают при разборе «почему нет сигналов».
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

RunStatus = Literal["running", "ok", "partial", "failed"]


class RunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_daily_stages(self, run_date: dt.date) -> dict[str, bool]:
        """Return durable completion flags for the daily pipeline."""
        result = await self._session.execute(
            text("""
                select collect_done, classify_done, analyze_done, notify_done
                  from wb.daily_job_stages
                 where run_date = :run_date
            """),
            {"run_date": run_date},
        )
        row = result.first()
        if row is None:
            return {"collect": False, "classify": False, "analyze": False, "notify": False}
        return {
            "collect": row.collect_done,
            "classify": row.classify_done,
            "analyze": row.analyze_done,
            "notify": row.notify_done,
        }

    async def mark_daily_stage(self, run_date: dt.date, stage: str) -> None:
        """Persist successful completion of one pipeline stage."""
        columns = {
            "collect": "collect_done",
            "classify": "classify_done",
            "analyze": "analyze_done",
            "notify": "notify_done",
        }
        column = columns.get(stage)
        if column is None:
            raise ValueError(f"Неизвестный этап ежедневного прогона: {stage}")
        await self._session.execute(
            text(f"""
                insert into wb.daily_job_stages (run_date, {column})
                values (:run_date, true)
                on conflict (run_date) do update set {column} = true
            """),
            {"run_date": run_date},
        )

    async def record_classify_status(
        self, run_date: dt.date, status: str, detail: str | None = None
    ) -> None:
        """Record a completed or deferred AI attempt without hiding later stages."""
        valid = {
            "complete",
            "partial",
            "disabled",
            "deferred_budget",
            "deferred_provider",
            "deferred_config",
            "deferred_limit",
        }
        if status not in valid:
            raise ValueError(f"Неизвестный результат AI-классификации: {status}")
        await self._session.execute(
            text("""
                insert into wb.daily_job_stages
                    (run_date, classify_done, classify_status, classify_detail, updated_at)
                values (:run_date, :done, :status, :detail, now())
                on conflict (run_date) do update
                   set classify_done = excluded.classify_done,
                       classify_status = excluded.classify_status,
                       classify_detail = excluded.classify_detail,
                       updated_at = now()
            """),
            {
                "run_date": run_date,
                "done": status in {"complete", "partial", "disabled"},
                "status": status,
                "detail": detail,
            },
        )

    async def unfinished_daily_dates(self, *, until: dt.date) -> set[dt.date]:
        result = await self._session.execute(
            text("""
                select run_date from wb.daily_job_stages
                 where run_date <= :until
                   and not (collect_done and classify_done and analyze_done and notify_done)
                union
                select distinct c.run_date from wb.collection_runs c
                 where c.run_date <= :until
                   and not exists (
                       select 1 from wb.daily_job_stages s where s.run_date = c.run_date
                   )
            """),
            {"until": until},
        )
        return {row.run_date for row in result.all()}

    async def complete_dates(
        self, account_keys: tuple[str, ...], *, until: dt.date
    ) -> set[dt.date]:
        """Даты, для которых все ожидаемые кабинеты имеют успешный прогон."""
        if not account_keys:
            return set()
        result = await self._session.execute(
            text("""
                select run_date
                  from wb.collection_runs
                 where run_date <= :until
                   and account_key = any(:account_keys)
                   and status = 'ok'
                 group by run_date
                having count(distinct account_key) = :account_count
            """),
            {
                "until": until,
                "account_keys": list(account_keys),
                "account_count": len(account_keys),
            },
        )
        return {row.run_date for row in result.all()}

    async def start(
        self, run_date: dt.date, started_at: dt.datetime, account_key: str = "default"
    ) -> int:
        await self._session.execute(
            text("""
                update wb.collection_runs
                   set status = 'failed',
                       finished_at = :started_at,
                       error = coalesce(error, 'Прерван до завершения; начат повторный сбор')
                 where run_date = :run_date
                   and account_key = :account_key
                   and status = 'running'
            """),
            {"run_date": run_date, "started_at": started_at, "account_key": account_key},
        )
        result = await self._session.execute(
            text("""
                insert into wb.collection_runs (run_date, started_at, status, account_key)
                values (:run_date, :started_at, 'running', :account_key)
                returning id
            """),
            {"run_date": run_date, "started_at": started_at, "account_key": account_key},
        )
        return int(result.scalar_one())

    async def finish(
        self,
        run_id: int,
        *,
        finished_at: dt.datetime,
        status: RunStatus,
        products_ok: int = 0,
        products_failed: int = 0,
        error: str | None = None,
    ) -> None:
        await self._session.execute(
            text("""
                update wb.collection_runs
                   set finished_at = :finished_at,
                       status = :status,
                       products_ok = :products_ok,
                       products_failed = :products_failed,
                       error = :error
                 where id = :id
            """),
            {
                "id": run_id,
                "finished_at": finished_at,
                "status": status,
                "products_ok": products_ok,
                "products_failed": products_failed,
                "error": error,
            },
        )
