"""Данные отчёта 1: границы периода, несколько SKU и пропущенные снимки."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def test_spec_fewer_five_star_ratings_can_mean_a_growing_share(
    session: AsyncSession,
) -> None:
    from app.repositories.reporting import load_rating_report

    await session.execute(
        text("""insert into wb.products(nm_id,vendor_code,updated_at)
        values (999001006,'SPEC-SHARE',now())""")
    )
    for day, total, five, four in [(8, 0, 0, 0), (9, 200, 100, 100), (10, 250, 150, 100)]:
        await session.execute(
            text("""insert into wb.daily_snapshots
                (nm_id,snapshot_date,rating,ratings_total,ratings_5,ratings_4,
                 ratings_3,ratings_2,ratings_1,collected_at)
                values (999001006,:day,4.8,:total,:five,:four,0,0,0,now())"""),
            {"day": dt.date(2026, 9, day), "total": total, "five": five, "four": four},
        )
    report = await load_rating_report(
        session, start=dt.date(2026, 9, 10), end=dt.date(2026, 9, 10), nm_ids=[999001006]
    )
    comparison = report.rows[0].comparison
    assert comparison is not None
    assert comparison.previous.counts["5"] == 100
    assert comparison.current.counts["5"] == 50
    assert comparison.count_changes_pct["5"].value == -50
    assert comparison.previous.shares["5"] == 0.5
    assert comparison.current.shares["5"] == 1.0
    assert comparison.share_changes_pct["5"].value == 100
    assert comparison.share_changes_pp["5"].value == 50


async def test_report_uses_day_before_period_and_keeps_missing_baseline_visible(
    session: AsyncSession,
) -> None:
    from app.repositories.reporting import load_rating_report

    for nm_id in (999_001_001, 999_001_002):
        await session.execute(
            text("""
                insert into wb.products (nm_id, vendor_code, title, updated_at)
                values (:nm, :code, :title, now())
            """),
            {
                "nm": nm_id,
                "code": f"REPORT-{nm_id}",
                "title": "Тестовая карточка" if nm_id == 999_001_001 else None,
            },
        )
    points = [
        (999_001_001, dt.date(2026, 9, 9), 4.90, 100, 70, 20, 5, 3, 2),
        (999_001_001, dt.date(2026, 9, 10), 4.85, 110, 76, 23, 5, 4, 2),
        (999_001_001, dt.date(2026, 9, 11), 4.80, 120, 80, 25, 6, 6, 3),
        (999_001_002, dt.date(2026, 9, 11), 4.60, 5, 2, 1, 1, 1, 0),
    ]
    for nm_id, date, rating, total, five, four, three, two, one in points:
        await session.execute(
            text("""
                insert into wb.daily_snapshots (
                    nm_id, snapshot_date, rating, ratings_total,
                    ratings_5, ratings_4, ratings_3, ratings_2, ratings_1,
                    collected_at
                ) values (:nm, :date, :rating, :total, :five, :four,
                          :three, :two, :one, now())
            """),
            {
                "nm": nm_id,
                "date": date,
                "rating": rating,
                "total": total,
                "five": five,
                "four": four,
                "three": three,
                "two": two,
                "one": one,
            },
        )

    report = await load_rating_report(
        session,
        start=dt.date(2026, 9, 10),
        end=dt.date(2026, 9, 11),
        nm_ids=[999_001_001, 999_001_002],
    )

    assert len(report.rows) == 2
    first, second = report.rows
    assert first.rating_start == 4.9 and first.rating_end == 4.8
    assert first.rating_delta == -0.1
    assert first.title == "Тестовая карточка"
    assert second.title is None
    assert first.ratings_5_delta == 10
    assert first.ratings_1_delta == 1
    assert report.ratings_by_day[first.nm_id] == (4.85, 4.8)
    assert second.rating_start is None and second.rating_end == 4.6
    assert second.rating_delta is None and second.ratings_5_delta is None

    filtered = await load_rating_report(
        session,
        start=dt.date(2026, 9, 10),
        end=dt.date(2026, 9, 11),
        nm_ids=[999_001_002],
    )
    assert [row.nm_id for row in filtered.rows] == [999_001_002]


async def test_report_compares_adjacent_periods_with_counts_shares_rating_and_buyout(
    session: AsyncSession,
) -> None:
    from app.repositories.reporting import load_rating_report

    nm_id = 999_001_003
    await session.execute(
        text("""insert into wb.products (nm_id, vendor_code, updated_at)
             values (:nm, 'REPORT-COMP', now())"""),
        {"nm": nm_id},
    )
    # Baseline, prior end, first current day, and current end. Every bucket sum
    # matches the cumulative total so these are valid snapshots.
    points = [
        (dt.date(2026, 9, 7), 4.80, 100, 70, 15, 7, 5, 3, 85.0),
        (dt.date(2026, 9, 9), 4.85, 110, 80, 15, 7, 5, 3, 85.0),
        (dt.date(2026, 9, 10), 4.90, 117, 83, 16, 8, 6, 4, 83.0),
        (dt.date(2026, 9, 11), 4.70, 130, 90, 20, 10, 6, 4, 81.0),
    ]
    for day, rating, total, five, four, three, two, one, buyout in points:
        await session.execute(
            text("""insert into wb.daily_snapshots (
                nm_id, snapshot_date, rating, ratings_total, ratings_5, ratings_4,
                ratings_3, ratings_2, ratings_1, buyout_percent, collected_at
            ) values (:nm, :day, :rating, :total, :five, :four, :three, :two,
                      :one, :buyout, now())"""),
            {
                "nm": nm_id,
                "day": day,
                "rating": rating,
                "total": total,
                "five": five,
                "four": four,
                "three": three,
                "two": two,
                "one": one,
                "buyout": buyout,
            },
        )

    report = await load_rating_report(
        session,
        start=dt.date(2026, 9, 10),
        end=dt.date(2026, 9, 11),
        nm_ids=[nm_id],
        include_daily=True,
    )
    row = report.rows[0]
    comparison = row.comparison
    assert (report.previous_start, report.previous_end) == (
        dt.date(2026, 9, 8),
        dt.date(2026, 9, 9),
    )
    assert comparison.previous.counts == {
        "total": 10,
        "5": 10,
        "4": 0,
        "3": 0,
        "2": 0,
        "1": 0,
        "negative": 0,
    }
    assert comparison.current.counts == {
        "total": 20,
        "5": 10,
        "4": 5,
        "3": 3,
        "2": 1,
        "1": 1,
        "negative": 5,
    }
    assert comparison.current.shares["5"] == 0.5
    assert comparison.current.shares["negative"] == 0.25
    assert comparison.share_changes_pp["5"].value == -50.0
    assert comparison.count_changes_pct["total"].value == 100.0
    assert comparison.rating_change_pct.value == pytest.approx((4.70 / 4.85 - 1) * 100)
    assert comparison.buyout_previous == 85.0 and comparison.buyout_current == 81.0
    assert comparison.buyout_delta_pp.value == -4.0
    assert report.daily_by_product[nm_id][0].counts["total"] == 7
    assert report.daily_by_product[nm_id][0].status == "ok"
    summary = await load_rating_report(
        session,
        start=report.start,
        end=report.end,
        nm_ids=[nm_id],
        include_rating_history=False,
    )
    assert summary.rows == report.rows
    assert summary.ratings_by_day == {}
    assert summary.dates == report.dates
    full_daily = await load_rating_report(
        session,
        start=report.start,
        end=report.end,
        nm_ids=[nm_id],
        include_daily=True,
        include_rating_history=False,
    )
    assert full_daily == report


async def test_product_snapshot_daily_uses_boundary_and_does_not_bridge_gaps(
    session: AsyncSession,
) -> None:
    from app.repositories.monitoring import product_snapshots

    nm_id = 999_001_004
    await session.execute(
        text("""insert into wb.products (nm_id, vendor_code, updated_at)
             values (:nm, 'SNAP-COMP', now())"""),
        {"nm": nm_id},
    )
    points = [
        (dt.date(2026, 9, 9), 100, 70, 15, 7, 5, 3),
        (dt.date(2026, 9, 10), 110, 80, 15, 7, 5, 3),
        (dt.date(2026, 9, 12), 130, 90, 20, 10, 6, 4),
    ]
    for day, total, five, four, three, two, one in points:
        await session.execute(
            text("""insert into wb.daily_snapshots (
                nm_id, snapshot_date, rating, ratings_total, ratings_5, ratings_4,
                ratings_3, ratings_2, ratings_1, collected_at
            ) values (:nm, :day, 4.8, :total, :five, :four, :three, :two, :one, now())"""),
            {
                "nm": nm_id,
                "day": day,
                "total": total,
                "five": five,
                "four": four,
                "three": three,
                "two": two,
                "one": one,
            },
        )
    result = await product_snapshots(
        session, nm_id=nm_id, start=dt.date(2026, 9, 10), end=dt.date(2026, 9, 12)
    )
    assert [row["snapshot_date"] for row in result["snapshots"]] == [
        dt.date(2026, 9, 10),
        dt.date(2026, 9, 12),
    ]
    assert result["daily"][0]["counts"]["total"] == 10
    assert result["daily"][0]["status"] == "ok"
    assert result["daily"][1]["status"] == "missing"
    assert result["daily"][2]["status"] == "missing"


async def test_counter_correction_does_not_hide_rating_or_buyout_change(
    session: AsyncSession,
) -> None:
    from app.repositories.reporting import load_rating_report

    nm_id = 999_001_005
    await session.execute(
        text("""insert into wb.products (nm_id, vendor_code, updated_at)
             values (:nm, 'REPORT-CORR', now())"""),
        {"nm": nm_id},
    )
    for day, rating, total, five, four, three, two, one, buyout in [
        (dt.date(2026, 9, 19), 4.80, 100, 70, 15, 7, 5, 3, 88.0),
        # One missing rating in the buckets marks derived count metrics as a correction.
        (dt.date(2026, 9, 20), 4.90, 110, 77, 16, 8, 5, 3, 89.0),
    ]:
        await session.execute(
            text("""insert into wb.daily_snapshots (
                nm_id, snapshot_date, rating, ratings_total, ratings_5, ratings_4,
                ratings_3, ratings_2, ratings_1, buyout_percent, collected_at
            ) values (:nm, :day, :rating, :total, :five, :four, :three, :two,
                      :one, :buyout, now())"""),
            {
                "nm": nm_id,
                "day": day,
                "rating": rating,
                "total": total,
                "five": five,
                "four": four,
                "three": three,
                "two": two,
                "one": one,
                "buyout": buyout,
            },
        )
    report = await load_rating_report(
        session, start=dt.date(2026, 9, 20), end=dt.date(2026, 9, 20), nm_ids=[nm_id]
    )
    comparison = report.rows[0].comparison
    assert comparison.current.status == "correction"
    assert comparison.current.shares["5"] is None
    assert comparison.rating_change_pct.value == pytest.approx((4.90 / 4.80 - 1) * 100)
    assert comparison.buyout_delta_pp.value == 1.0
    assert comparison.share_changes_pp["5"].status == "correction"
