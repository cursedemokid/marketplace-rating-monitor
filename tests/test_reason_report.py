"""Отчёт причин: знаменатель — все негативные отзывы выбранных артикулов."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def test_reason_report_counts_multi_label_reviews_against_all_negative_reviews(
    session: AsyncSession,
) -> None:
    from app.repositories.reporting import load_reason_report

    await session.execute(
        text("""
            insert into wb.products (nm_id, vendor_code, updated_at)
            values (2763497750, 'CAUSE-A', now()), (3597807103, 'CAUSE-B', now())
        """)
    )
    reasons = (
        await session.execute(
            text("""
                select id, name from wb.negative_reasons
                 where name in ('протекают', 'маломерят')
            """)
        )
    ).all()
    reason_ids = {str(row.name): int(row.id) for row in reasons}
    assert len(reason_ids) == 2
    await session.execute(
        text("""
            insert into wb.negative_reasons (name, created_by, is_approved)
            values ('ожидает модерации отчёта', 'ai', false)
        """)
    )
    pending_id = (
        await session.execute(
            text("select id from wb.negative_reasons where name = 'ожидает модерации отчёта'")
        )
    ).scalar_one()
    feedbacks = [
        ("cause-1", 2763497750, 1, dt.date(2026, 9, 10)),
        ("cause-2", 2763497750, 2, dt.date(2026, 9, 10)),
        ("cause-3", 2763497750, 5, dt.date(2026, 9, 10)),
        ("cause-4", 3597807103, 3, dt.date(2026, 9, 10)),
        ("cause-5", 2763497750, 1, dt.date(2026, 9, 11)),
    ]
    for feedback_id, nm_id, stars, day in feedbacks:
        await session.execute(
            text("""
                insert into wb.feedbacks
                    (id, nm_id, valuation, created_at, created_date, imported_at)
                values (:id, :nm, :stars, :created, :day, now())
            """),
            {
                "id": feedback_id,
                "nm": nm_id,
                "stars": stars,
                "created": dt.datetime.combine(day, dt.time(12), dt.UTC),
                "day": day,
            },
        )
    for feedback_id, reason_id in [
        ("cause-1", reason_ids["протекают"]),
        ("cause-1", reason_ids["маломерят"]),
        ("cause-2", reason_ids["протекают"]),
        ("cause-3", reason_ids["маломерят"]),  # позитивный отзыв исключён
        ("cause-4", reason_ids["маломерят"]),
        ("cause-5", pending_id),  # неподтверждённая причина исключена
    ]:
        await session.execute(
            text("""
                insert into wb.feedback_reasons
                    (feedback_id, reason_id, model, classified_at)
                values (:feedback, :reason, 'test', now())
            """),
            {"feedback": feedback_id, "reason": reason_id},
        )

    report = await load_reason_report(
        session,
        start=dt.date(2026, 9, 10),
        end=dt.date(2026, 9, 12),
        nm_ids=[2763497750, 3597807103],
    )
    by_name = {row.name: row for row in report.rows}
    assert report.negative_counts == (3, 1, 0)
    assert by_name["протекают"].counts == (2, 0, 0)
    assert by_name["протекают"].shares == (2 / 3, 0.0, None)
    assert by_name["маломерят"].counts == (2, 0, 0)
    assert by_name["маломерят"].shares == (2 / 3, 0.0, None)
    assert report.identified_counts == (3, 0, 0)
    assert report.total_shares == (1.0, 0.0, None)
    assert "ожидает модерации отчёта" not in by_name

    filtered = await load_reason_report(
        session,
        start=dt.date(2026, 9, 10),
        end=dt.date(2026, 9, 10),
        nm_ids=[2763497750],
    )
    assert filtered.negative_counts == (2,)
    assert filtered.total_shares == (1.0,)
    assert {row.name: row.shares[0] for row in filtered.rows if row.name in reason_ids} == {
        "протекают": 1.0,
        "маломерят": 0.5,
    }


async def test_reason_report_rejects_reversed_period(session: AsyncSession) -> None:
    from app.repositories.reporting import load_reason_report

    try:
        await load_reason_report(session, start=dt.date(2026, 9, 12), end=dt.date(2026, 9, 10))
    except ValueError as exc:
        assert "Конец периода" in str(exc)
    else:
        raise AssertionError("Инвертированный период должен отклоняться")


async def test_external_reason_is_visible_without_increasing_identified_reviews(
    session: AsyncSession,
) -> None:
    from app.repositories.reasons import ReasonRepository
    from app.repositories.reporting import load_reason_report

    await session.execute(
        text("""
            insert into wb.products (nm_id, vendor_code, updated_at)
            values (4833142013, 'PVZ', now())
        """)
    )
    pvz_id = (
        await session.execute(text("select id from wb.negative_reasons where name = 'работа ПВЗ'"))
    ).scalar_one()
    product_id = (
        await session.execute(text("select id from wb.negative_reasons where name = 'протекают'"))
    ).scalar_one()
    unspecified_id = (
        await session.execute(
            text("select id from wb.negative_reasons where name = 'причина не указана'")
        )
    ).scalar_one()
    await session.execute(
        text("""
            insert into wb.feedbacks
                (id, nm_id, valuation, created_at, created_date, imported_at)
            values ('pvz-only', 4833142013, 1, 'DemoPhoneT12:00:00Z', 'DemoPhone', now()),
                   ('pvz-mixed', 4833142013, 2, 'DemoPhoneT13:00:00Z', 'DemoPhone', now()),
                   ('unspecified', 4833142013, 3, 'DemoPhoneT14:00:00Z', 'DemoPhone', now())
        """)
    )
    await session.execute(
        text("""
            insert into wb.feedback_reasons (feedback_id, reason_id, model, classified_at)
            values ('pvz-only', :pvz, 'test', now()),
                   ('pvz-mixed', :pvz, 'test', now()),
                   ('pvz-mixed', :product, 'test', now()),
                   ('unspecified', :unspecified, 'test', now())
        """),
        {"pvz": pvz_id, "product": product_id, "unspecified": unspecified_id},
    )

    report = await load_reason_report(
        session, start=dt.date(2026, 10, 16), end=dt.date(2026, 10, 16), nm_ids=[4833142013]
    )
    by_name = {row.name: row for row in report.rows}
    assert by_name["работа ПВЗ"].counts == (2,)
    assert by_name["работа ПВЗ"].shares == (2 / 3,)
    assert by_name["работа ПВЗ"].is_external
    assert by_name["причина не указана"].counts == (1,)
    assert by_name["причина не указана"].shares == (1 / 3,)
    assert report.negative_counts == (3,)
    assert report.identified_counts == (1,)
    assert report.total_shares == (1 / 3,)
    assert by_name["протекают"].counts == (1,)
    assert "работа ПВЗ" in (await ReasonRepository(session).approved()).values()


async def test_top_reason_excludes_external_reasons(session: AsyncSession) -> None:
    from app.repositories.reasons import ReasonRepository

    await session.execute(
        text("""
            insert into wb.products (nm_id, vendor_code, updated_at)
            values (5179408960, 'PVZ-TOP', now())
        """)
    )
    external_id = (
        await session.execute(text("select id from wb.negative_reasons where name = 'работа ПВЗ'"))
    ).scalar_one()
    await session.execute(
        text("""
            insert into wb.feedbacks
                (id, nm_id, valuation, created_at, created_date, imported_at)
            values ('pvz-top', 5179408960, 1, 'DemoPhoneT12:00:00Z', 'DemoPhone', now())
        """)
    )
    await session.execute(
        text("""
            insert into wb.feedback_reasons (feedback_id, reason_id, model, classified_at)
            values ('pvz-top', :rid, 'test', now())
        """),
        {"rid": external_id},
    )
    assert (
        await ReasonRepository(session).top_reason(
            nm_id=5179408960, period_start=dt.date(2026, 10, 16), period_end=dt.date(2026, 10, 16)
        )
        is None
    )
