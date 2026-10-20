"""Общая выборка динамики рейтингов для API и Excel-отчёта 1."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.comparison import (
    COUNT_KEYS,
    SHARE_KEYS,
    Change,
    Comparison,
    DailyPoint,
    PeriodMetrics,
    change,
    metrics,
    previous_bounds,
)


@dataclass(frozen=True, slots=True)
class RatingReportRow:
    nm_id: int
    vendor_code: str
    rating_start: float | None
    rating_end: float | None
    rating_delta: float | None
    ratings_5_delta: int | None
    ratings_4_delta: int | None
    ratings_3_delta: int | None
    ratings_2_delta: int | None
    ratings_1_delta: int | None
    account_key: str = "default"
    title: str | None = None
    comparison: Comparison | None = None


@dataclass(frozen=True, slots=True)
class RatingReport:
    start: dt.date
    end: dt.date
    dates: tuple[dt.date, ...]
    rows: tuple[RatingReportRow, ...]
    ratings_by_day: dict[int, tuple[float | None, ...]]
    previous_start: dt.date | None = None
    previous_end: dt.date | None = None
    daily_by_product: dict[int, tuple[DailyPoint, ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReasonReportRow:
    reason_id: int
    name: str
    counts: tuple[int, ...]
    shares: tuple[float | None, ...]
    is_external: bool = False


@dataclass(frozen=True, slots=True)
class ReasonReport:
    start: dt.date
    end: dt.date
    dates: tuple[dt.date, ...]
    rows: tuple[ReasonReportRow, ...]
    negative_counts: tuple[int, ...]
    identified_counts: tuple[int, ...]
    total_shares: tuple[float | None, ...]


def _float(value: Decimal | float | int | None) -> float | None:
    return None if value is None else float(value)


async def load_rating_report(
    session: AsyncSession,
    *,
    start: dt.date,
    end: dt.date,
    nm_ids: list[int] | None = None,
    include_daily: bool = False,
    include_rating_history: bool = True,
) -> RatingReport:
    """На начало берём снимок накануне периода, как в `counts_for_period`."""
    if end < start:
        raise ValueError("Конец периода раньше начала")
    dates = tuple(start + dt.timedelta(days=day) for day in range((end - start).days + 1))
    selected = "and p.nm_id = any(:nm_ids)" if nm_ids is not None else ""
    result = await session.execute(
        text(f"""
            select p.nm_id, p.vendor_code, p.account_key, p.title,
                   s0.rating as rating_start, s1.rating as rating_end,
                   case when s0.nm_id is not null and s1.nm_id is not null
                        then s1.rating - s0.rating end as rating_delta,
                   case when s0.nm_id is not null and s1.nm_id is not null
                        then s1.ratings_5 - s0.ratings_5 end as ratings_5_delta,
                   case when s0.nm_id is not null and s1.nm_id is not null
                        then s1.ratings_4 - s0.ratings_4 end as ratings_4_delta,
                   case when s0.nm_id is not null and s1.nm_id is not null
                        then s1.ratings_3 - s0.ratings_3 end as ratings_3_delta,
                   case when s0.nm_id is not null and s1.nm_id is not null
                        then s1.ratings_2 - s0.ratings_2 end as ratings_2_delta,
                   case when s0.nm_id is not null and s1.nm_id is not null
                        then s1.ratings_1 - s0.ratings_1 end as ratings_1_delta
              from wb.products p
              left join wb.daily_snapshots s0
                on s0.nm_id = p.nm_id and s0.snapshot_date = :before
              left join wb.daily_snapshots s1
                on s1.nm_id = p.nm_id and s1.snapshot_date = :end
             where p.is_monitored {selected}
             order by p.vendor_code, p.nm_id
        """),
        {"before": start - dt.timedelta(days=1), "end": end, "nm_ids": nm_ids},
    )
    raw_rows = result.all()
    previous_start, previous_end = previous_bounds(start, end)
    snapshots: dict[int, dict[dt.date, Any]] = {}
    daily: dict[int, dict[dt.date, Any]] = {}
    if raw_rows:
        result = await session.execute(
            text("""
                select nm_id, snapshot_date, rating, ratings_total,
                       ratings_5, ratings_4, ratings_3, ratings_2, ratings_1, buyout_percent
                  from wb.daily_snapshots
                 where nm_id = any(:ids) and snapshot_date = any(:boundaries)
            """),
            {
                "ids": [int(row.nm_id) for row in raw_rows],
                "boundaries": [previous_start - dt.timedelta(days=1), previous_end, end],
            },
        )
        for snap in result.all():
            snapshots.setdefault(int(snap.nm_id), {})[snap.snapshot_date] = snap
    # Only boundaries are needed for comparison. The optional legacy history
    # needs ratings alone, while daily charts/export also need full counters.
    # The wide table opts out of unused history without dropping any metrics.
    if raw_rows and (include_rating_history or include_daily):
        columns = (
            "ratings_total, ratings_5, ratings_4, ratings_3, ratings_2, ratings_1, buyout_percent,"
            if include_daily
            else ""
        )
        result = await session.execute(
            text(f"""
                select nm_id, snapshot_date, {columns} rating
                  from wb.daily_snapshots
                 where nm_id = any(:ids) and snapshot_date between :first and :last
            """),
            {
                "ids": [int(row.nm_id) for row in raw_rows],
                "first": start - dt.timedelta(days=1),
                "last": end,
            },
        )
        for daily_row in result.all():
            daily.setdefault(int(daily_row.nm_id), {})[daily_row.snapshot_date] = daily_row

    def period_counts(
        points: dict[dt.date, Any], before: dt.date, after: dt.date
    ) -> dict[str, int | None]:
        left, right = points.get(before), points.get(after)
        if left is None or right is None:
            return dict.fromkeys(COUNT_KEYS)
        vals = {"total": right.ratings_total - left.ratings_total}
        vals.update(
            {
                key: getattr(right, f"ratings_{key}") - getattr(left, f"ratings_{key}")
                for key in ("5", "4", "3", "2", "1")
            }
        )
        vals["negative"] = vals["3"] + vals["2"] + vals["1"]
        return vals

    built = []
    for row in raw_rows:
        points = snapshots.get(int(row.nm_id), {})
        prev = metrics(period_counts(points, previous_start - dt.timedelta(days=1), previous_end))
        curr = metrics(period_counts(points, start - dt.timedelta(days=1), end))
        correction = prev.status == "correction" or curr.status == "correction"
        cnt_changes = {
            key: change(prev.counts[key], curr.counts[key], correction=correction)
            for key in COUNT_KEYS
        }
        pct_changes = {
            key: change(prev.shares[key], curr.shares[key], correction=correction)
            if prev.status != "empty" and curr.status != "empty"
            else Change(None, "correction" if correction else "empty")
            for key in SHARE_KEYS
        }

        def share_point_delta(
            key: str, previous: PeriodMetrics, current: PeriodMetrics, corrected: bool
        ) -> Change:
            before, after = previous.shares[key], current.shares[key]
            if corrected:
                return Change(None, "correction")
            if previous.status == "empty" or current.status == "empty":
                return Change(None, "empty")
            if before is None or after is None:
                return Change(None, "missing")
            return Change((after - before) * 100, "ok")

        pp_changes = {key: share_point_delta(key, prev, curr, correction) for key in SHARE_KEYS}
        prev_snap, curr_snap = points.get(previous_end), points.get(end)
        # Rating and buyout series remain valid even if rating count counters corrected.
        rating_pct = change(
            _float(prev_snap.rating) if prev_snap else None,
            _float(curr_snap.rating) if curr_snap else None,
        )
        buy_prev, buy_curr = (
            (_float(prev_snap.buyout_percent) if prev_snap else None),
            (_float(curr_snap.buyout_percent) if curr_snap else None),
        )
        buy_delta = Change(
            None if buy_prev is None or buy_curr is None else buy_curr - buy_prev,
            "missing" if buy_prev is None or buy_curr is None else "ok",
        )
        comp = Comparison(
            curr,
            prev,
            rating_pct,
            cnt_changes,
            pct_changes,
            pp_changes,
            buy_prev,
            buy_curr,
            buy_delta,
        )
        built.append(
            RatingReportRow(
                nm_id=int(row.nm_id),
                vendor_code=str(row.vendor_code),
                rating_start=_float(row.rating_start),
                rating_end=_float(row.rating_end),
                rating_delta=_float(row.rating_delta),
                ratings_5_delta=row.ratings_5_delta,
                ratings_4_delta=row.ratings_4_delta,
                ratings_3_delta=row.ratings_3_delta,
                ratings_2_delta=row.ratings_2_delta,
                ratings_1_delta=row.ratings_1_delta,
                account_key=str(row.account_key),
                title=row.title,
                comparison=comp,
            )
        )
    rows = tuple(built)
    ratings: dict[int, dict[dt.date, float | None]] = (
        {row.nm_id: {} for row in rows} if include_rating_history or include_daily else {}
    )
    for nm_id, points in daily.items():
        ratings[nm_id] = {day: _float(point.rating) for day, point in points.items()}
    daily_by_product: dict[int, tuple[DailyPoint, ...]] = {}
    if include_daily:
        for nm_id in ratings:
            series = []
            values = daily.get(nm_id, {})
            prev_day = start - dt.timedelta(days=1)
            prev_snap = values.get(prev_day)
            for day in dates:
                point = values.get(day)
                if prev_snap is None or point is None:
                    met = metrics(dict.fromkeys(COUNT_KEYS))
                else:
                    met = metrics(
                        {
                            "total": point.ratings_total - prev_snap.ratings_total,
                            **{
                                k: getattr(point, f"ratings_{k}")
                                - getattr(prev_snap, f"ratings_{k}")
                                for k in ("5", "4", "3", "2", "1")
                            },
                        }
                    )
                series.append(DailyPoint(day, met.counts, met.shares, met.status))
                prev_day, prev_snap = day, point
            daily_by_product[nm_id] = tuple(series)
    return RatingReport(
        start=start,
        end=end,
        dates=dates,
        rows=rows,
        ratings_by_day={
            nm_id: tuple(values.get(day) for day in dates) for nm_id, values in ratings.items()
        },
        previous_start=previous_start,
        previous_end=previous_end,
        daily_by_product=daily_by_product if include_daily else {},
    )


async def load_reason_report(
    session: AsyncSession,
    *,
    start: dt.date,
    end: dt.date,
    nm_ids: list[int] | None = None,
) -> ReasonReport:
    """Доля каждой причины среди всех негативных отзывов за дату.

    Итог учитывает каждый отзыв с известной подтверждённой причиной один раз.
    «Причина не указана» и внешние причины видны отдельно, но не входят в итог.
    """
    if end < start:
        raise ValueError("Конец периода раньше начала")
    dates = tuple(start + dt.timedelta(days=day) for day in range((end - start).days + 1))
    selected = "and p.nm_id = any(:nm_ids)" if nm_ids is not None else ""
    params = {"start": start, "end": end, "nm_ids": nm_ids}

    reasons_result = await session.execute(
        text("""
            select id, name, is_external from wb.negative_reasons
             where is_active and is_approved order by name, id
        """)
    )
    reasons = [(int(row.id), str(row.name), bool(row.is_external)) for row in reasons_result.all()]

    negatives_result = await session.execute(
        text(f"""
            select f.created_date, count(*) as review_count
              from wb.feedbacks f
              join wb.products p on p.nm_id = f.nm_id
             where p.is_monitored {selected}
               and f.valuation <= 3
               and f.created_date between :start and :end
             group by f.created_date
        """),
        params,
    )
    negative_by_day = {row.created_date: int(row.review_count) for row in negatives_result.all()}

    assigned_result = await session.execute(
        text(f"""
            select f.created_date, r.id as reason_id, count(distinct f.id) as review_count
              from wb.feedbacks f
              join wb.products p on p.nm_id = f.nm_id
              join wb.feedback_reasons fr on fr.feedback_id = f.id
              join wb.negative_reasons r on r.id = fr.reason_id
             where p.is_monitored {selected}
               and f.valuation <= 3
               and f.created_date between :start and :end
               and r.is_active and r.is_approved
             group by f.created_date, r.id
        """),
        params,
    )
    assigned = {
        (row.created_date, int(row.reason_id)): int(row.review_count)
        for row in assigned_result.all()
    }

    identified_result = await session.execute(
        text(f"""
            select f.created_date, count(distinct f.id) as review_count
              from wb.feedbacks f
              join wb.products p on p.nm_id = f.nm_id
              join wb.feedback_reasons fr on fr.feedback_id = f.id
              join wb.negative_reasons r on r.id = fr.reason_id
             where p.is_monitored {selected}
               and f.valuation <= 3
               and f.created_date between :start and :end
               and r.is_active and r.is_approved
               and not r.is_external
               and r.name <> 'причина не указана'
             group by f.created_date
        """),
        params,
    )
    identified_by_day = {row.created_date: int(row.review_count) for row in identified_result.all()}
    negative_counts = tuple(negative_by_day.get(day, 0) for day in dates)
    identified_counts = tuple(identified_by_day.get(day, 0) for day in dates)
    return ReasonReport(
        start=start,
        end=end,
        dates=dates,
        rows=tuple(
            ReasonReportRow(
                reason_id=reason_id,
                name=name,
                counts=tuple(assigned.get((day, reason_id), 0) for day in dates),
                shares=tuple(
                    assigned.get((day, reason_id), 0) / total if total else None
                    for day, total in zip(dates, negative_counts, strict=True)
                ),
                is_external=is_external,
            )
            for reason_id, name, is_external in reasons
        ),
        negative_counts=negative_counts,
        identified_counts=identified_counts,
        total_shares=tuple(
            identified / total if total else None
            for identified, total in zip(identified_counts, negative_counts, strict=True)
        ),
    )
