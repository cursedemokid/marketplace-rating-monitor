"""Короткий список проблем и история сигналов для рабочего интерфейса."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.comparison import COUNT_KEYS, metrics

_PRODUCT_SEARCH = "(position(lower(:query) in lower(p.vendor_code)) > 0 or s.nm_id::text = :query)"


async def attention(
    session: AsyncSession,
    *,
    since: dt.date,
    until: dt.date,
    query: str | None,
    limit: int,
    nm_ids: list[int] | None = None,
) -> list[dict[str, object]]:
    """Последний неподавленный сигнал каждого артикула за выбранное окно."""
    selected = f"and {_PRODUCT_SEARCH}" if query else ""
    if nm_ids is not None:
        selected += " and s.nm_id = any(:nm_ids)"
    result = await session.execute(
        text(f"""
            with ranked as (
                select s.id, s.nm_id, p.vendor_code, p.account_key, s.signal_type,
                       s.period_end, s.delta, s.ai_summary, s.created_at,
                       row_number() over (
                           partition by s.nm_id order by s.created_at desc, s.id desc
                       ) as position
                  from wb.signals s
                  join wb.products p on p.nm_id = s.nm_id
                 where p.is_monitored
                   and not s.is_suppressed
                   and s.period_end between :since and :until
                   {selected}
            )
            select id, nm_id, vendor_code, account_key, signal_type, period_end,
                   delta, ai_summary, created_at
              from ranked where position = 1
             order by created_at desc, id desc
             limit :limit
        """),
        {"since": since, "until": until, "query": query, "limit": limit, "nm_ids": nm_ids},
    )
    return [dict(row._mapping) for row in result.all()]


async def signal_history(
    session: AsyncSession,
    *,
    nm_id: int | None,
    signal_type: str | None,
    query: str | None,
    since: dt.date | None,
    until: dt.date | None,
    limit: int,
    offset: int,
    nm_ids: list[int] | None = None,
) -> list[dict[str, object]]:
    """История включает подавленные и пробные сигналы с честным статусом."""
    filters = []
    params: dict[str, object] = {"limit": limit, "offset": offset}
    if nm_ids is not None:
        filters.append("s.nm_id = any(:nm_ids)")
        params["nm_ids"] = nm_ids
    if nm_id is not None:
        filters.append("s.nm_id = :nm_id")
        params["nm_id"] = nm_id
    if signal_type is not None:
        filters.append("s.signal_type = :signal_type")
        params["signal_type"] = signal_type
    if query:
        filters.append(_PRODUCT_SEARCH)
        params["query"] = query
    if since is not None:
        filters.append("s.period_end >= :since")
        params["since"] = since
    if until is not None:
        filters.append("s.period_end <= :until")
        params["until"] = until
    where = " and ".join(filters) if filters else "true"
    result = await session.execute(
        text(f"""
            select s.id, s.nm_id, p.vendor_code, p.account_key, s.signal_type,
                   s.period_start, s.period_end, s.delta, s.ai_summary,
                   s.created_at, s.sent_at, s.is_suppressed, s.dry_run_at,
                   s.send_error
              from wb.signals s
              join wb.products p on p.nm_id = s.nm_id
             where p.is_monitored and {where}
             order by s.created_at desc, s.id desc
             limit :limit offset :offset
        """),
        params,
    )
    return [dict(row._mapping) for row in result.all()]


async def product_snapshots(
    session: AsyncSession, *, nm_id: int, start: dt.date, end: dt.date
) -> dict[str, object] | None:
    """Карточка и накопленные дневные точки для графиков рейтинга и выкупа."""
    if end < start:
        raise ValueError("Конец периода раньше начала")
    product_result = await session.execute(
        text("""
            select nm_id, vendor_code, title, brand, subject_name, size, account_key
              from wb.products where nm_id = :nm_id
        """),
        {"nm_id": nm_id},
    )
    product = product_result.one_or_none()
    if product is None:
        return None
    points_result = await session.execute(
        text("""
            select snapshot_date, rating, ratings_total,
                   ratings_5, ratings_4, ratings_3, ratings_2, ratings_1,
                   buyout_percent, orders_count, buyouts_count
              from wb.daily_snapshots
             where nm_id = :nm_id and snapshot_date between :baseline and :end
             order by snapshot_date
        """),
        {"nm_id": nm_id, "baseline": start - dt.timedelta(days=1), "end": end},
    )
    fetched_points = [dict(row._mapping) for row in points_result]
    by_day = {point["snapshot_date"]: point for point in fetched_points}
    points = [point for point in fetched_points if start <= point["snapshot_date"] <= end]
    daily = []
    previous_day = start - dt.timedelta(days=1)
    for day in (start + dt.timedelta(days=i) for i in range((end - start).days + 1)):
        left, right = by_day.get(previous_day), by_day.get(day)
        if left is None or right is None:
            computed = metrics(dict.fromkeys(COUNT_KEYS))
        else:
            computed = metrics(
                {
                    "total": right["ratings_total"] - left["ratings_total"],
                    **{
                        key: right[f"ratings_{key}"] - left[f"ratings_{key}"]
                        for key in ("5", "4", "3", "2", "1")
                    },
                }
            )
        daily.append(
            {
                "snapshot_date": day,
                "counts": computed.counts,
                "shares": computed.shares,
                "status": computed.status,
            }
        )
        previous_day = day
    return {**dict(product._mapping), "snapshots": points, "daily": daily}
