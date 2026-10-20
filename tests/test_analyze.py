"""Сквозной прогон анализа на живой базе. TSK-204, TSK-205, FR-3.6, FR-3.7."""

from __future__ import annotations

import datetime as dt
import zoneinfo

from app.clock import ManualClock
from app.repositories.signals import SignalRepository, SignalRow
from app.services import analyze
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MOSCOW = zoneinfo.ZoneInfo("Europe/Moscow")
NM_ID = 999_000_004
DAY = dt.date(2026, 9, 24)


async def seed_spike(session: AsyncSession) -> None:
    """Две недели спокойного фона и всплеск негатива в последний день.

    Снимки хранят накопленные счётчики, поэтому фон задаётся снимками
    за границы базового окна, а всплеск — приростом последнего дня.
    """
    await session.execute(
        text("""
            insert into wb.products (nm_id, vendor_code, updated_at)
            values (:nm, 'TEST-ANALYZE', now())
            on conflict (nm_id) do nothing
        """),
        {"nm": NM_ID},
    )
    rows = [
        # день перед базовым окном: точка отсчёта
        (dt.date(2026, 9, 9), 4.93, 1000, 900, 50, 20, 15, 15, 87.0, 5000),
        # конец базового окна: за 14 дней пришло 200 оценок, из них 10 негативных
        (dt.date(2026, 9, 23), 4.93, 1200, 1090, 60, 24, 18, 18, 87.0, 5200),
        # анализируемый день: 200 оценок, из них 40 негативных
        (DAY, 4.91, 1400, 1250, 70, 44, 28, 28, 86.7, 5400),
    ]
    for snapshot_date, rating, total, five, four, three, two, one, buyout, orders in rows:
        await session.execute(
            text("""
                insert into wb.daily_snapshots (
                    nm_id, snapshot_date, rating, ratings_total,
                    ratings_5, ratings_4, ratings_3, ratings_2, ratings_1,
                    buyout_percent, orders_count, collected_at
                ) values (:nm, :d, :rating, :total, :five, :four, :three, :two, :one,
                          :buyout, :orders, now())
                on conflict (nm_id, snapshot_date) do nothing
            """),
            {
                "nm": NM_ID,
                "d": snapshot_date,
                "rating": rating,
                "total": total,
                "five": five,
                "four": four,
                "three": three,
                "two": two,
                "one": one,
                "buyout": buyout,
                "orders": orders,
            },
        )


async def test_spike_creates_signal(session: AsyncSession) -> None:
    """Всплеск негатива находится, и по записи видно, почему он сработал."""
    await seed_spike(session)
    clock = ManualClock(dt.datetime(2026, 9, 25, 4, 30, tzinfo=MOSCOW), MOSCOW)

    result = await analyze.run(session, clock, run_date=DAY)

    assert result.products >= 1
    assert result.created >= 1

    row = (
        await session.execute(
            text("""
                select signal_type, value_before, value_after, delta, payload, sent_at
                  from wb.signals
                 where nm_id = :nm and signal_type = 'negative_spike' and period_end = :d
            """),
            {"nm": NM_ID, "d": DAY},
        )
    ).one()
    assert row.sent_at is None, "на этом этапе наружу ничего не уходит"
    assert float(row.delta) > 0
    payload = row.payload
    assert payload["marks_now"] == 40
    assert payload["marks_total_now"] == 200
    assert payload["marks_base"] == 10
    assert "z" in payload and "alpha" in payload
    assert payload["base_period"].startswith("10.09.2026")


async def test_spike_uses_most_common_approved_reason_in_summary(session: AsyncSession) -> None:
    await seed_spike(session)
    reason_id = (
        await session.execute(text("select id from wb.negative_reasons where name = 'протекают'"))
    ).scalar_one()
    for index in range(2):
        feedback_id = f"summary-{index}"
        await session.execute(
            text("""
                insert into wb.feedbacks (
                    id, nm_id, valuation, text, pros, cons,
                    created_at, created_date, imported_at
                ) values (:id, :nm, 1, 'Протекает', '', '', now(), :d, now())
            """),
            {"id": feedback_id, "nm": NM_ID, "d": DAY},
        )
        await session.execute(
            text("""
                insert into wb.feedback_reasons
                    (feedback_id, reason_id, model, classified_at)
                values (:id, :reason, 'test', now())
            """),
            {"id": feedback_id, "reason": reason_id},
        )
    clock = ManualClock(dt.datetime(2026, 9, 25, 4, 30, tzinfo=MOSCOW), MOSCOW)

    await analyze.run(session, clock, run_date=DAY)

    summary = (
        await session.execute(
            text("""
                select ai_summary from wb.signals
                 where nm_id = :nm and signal_type = 'negative_spike' and period_end = :d
            """),
            {"nm": NM_ID, "d": DAY},
        )
    ).scalar_one()
    assert summary == "протекают"


async def test_second_run_does_not_duplicate(session: AsyncSession) -> None:
    """Повторный прогон за ту же дату не плодит записи в истории."""
    await seed_spike(session)
    clock = ManualClock(dt.datetime(2026, 9, 25, 4, 30, tzinfo=MOSCOW), MOSCOW)
    repo = SignalRepository(session)

    await analyze.run(session, clock, run_date=DAY)
    after_first = await repo.count_for_period(DAY)
    await analyze.run(session, clock, run_date=DAY)
    after_second = await repo.count_for_period(DAY)

    assert after_second == after_first


async def test_disabled_rule_is_skipped(session: AsyncSession) -> None:
    """Выключенное правило не срабатывает — проверка флага is_enabled."""
    await seed_spike(session)
    await session.execute(
        text("update wb.signal_settings set is_enabled = false where signal_type <> 'x'")
    )
    clock = ManualClock(dt.datetime(2026, 9, 25, 4, 30, tzinfo=MOSCOW), MOSCOW)

    result = await analyze.run(session, clock, run_date=DAY)

    assert result.created == 0
    assert await SignalRepository(session).count_for_period(DAY) == 0


async def test_threshold_change_takes_effect_without_restart(session: AsyncSession) -> None:
    """AC-3: правка порога в базе меняет поведение со следующего прогона."""
    await seed_spike(session)
    clock = ManualClock(dt.datetime(2026, 9, 25, 4, 30, tzinfo=MOSCOW), MOSCOW)
    await session.execute(
        text("update wb.signal_settings set threshold = 50 where signal_type = 'negative_spike'")
    )

    await analyze.run(session, clock, run_date=DAY)

    spikes = (
        await session.execute(
            text("""
                select count(*) from wb.signals
                 where nm_id = :nm and signal_type = 'negative_spike' and period_end = :d
            """),
            {"nm": NM_ID, "d": DAY},
        )
    ).scalar_one()
    assert spikes == 0, "порог в 50 п.п. недостижим, сигнала быть не должно"


async def test_numbers_are_stored_without_binary_noise(session: AsyncSession) -> None:
    """В базе лежит 4.93, а не 4.4188101846.

    Числа уходят в numeric через Decimal: иначе двоичный хвост float
    доезжает до выгрузки, которую открывает заказчик.
    """
    await seed_spike(session)
    clock = ManualClock(dt.datetime(2026, 9, 25, 4, 30, tzinfo=MOSCOW), MOSCOW)

    await analyze.run(session, clock, run_date=DAY)

    row = (
        await session.execute(
            text("""
                select value_before, value_after, delta
                  from wb.signals
                 where nm_id = :nm and signal_type = 'rating_change' and period_end = :d
            """),
            {"nm": NM_ID, "d": DAY},
        )
    ).one()
    assert str(row.value_before) == "4.93"
    assert str(row.value_after) == "4.91"
    assert str(row.delta) == "-0.02"


async def test_suppressed_history_does_not_extend_suppression_window(
    session: AsyncSession,
) -> None:
    await session.execute(
        text("""
            insert into wb.products (nm_id, vendor_code, updated_at)
            values (:nm, 'TEST-SUPPRESS', now())
            on conflict (nm_id) do nothing
        """),
        {"nm": NM_ID},
    )
    repo = SignalRepository(session)
    now = dt.datetime(2026, 9, 24, 9, 0, tzinfo=MOSCOW)
    signal = SignalRow(
        nm_id=NM_ID,
        signal_type="negative_spike",
        period_start=DAY,
        period_end=DAY,
        value_before=5.0,
        value_after=20.0,
        delta=15.0,
        payload={},
    )
    await repo.add(signal, created_at=now - dt.timedelta(hours=48))
    await repo.add(
        signal,
        created_at=now - dt.timedelta(hours=1),
        is_suppressed=True,
    )

    assert not await repo.is_suppressed(NM_ID, "negative_spike", now=now, suppress_hours=24)


async def test_each_rule_uses_its_own_baseline_days(session: AsyncSession) -> None:
    await session.execute(
        text("""
            insert into wb.products (nm_id, vendor_code, updated_at)
            values (:nm, 'TEST-BASELINE', now())
            on conflict (nm_id) do nothing
        """),
        {"nm": NM_ID},
    )
    # В последние 7 дней негатив составляет 10/100, в последние 14 —
    # 70/200. Сегодня 40/100: всплеск есть только относительно 7 дней.
    points = [
        (dt.date(2026, 9, 9), 0, 0, 0),
        (dt.date(2026, 9, 16), 100, 40, 60),
        (dt.date(2026, 9, 23), 200, 130, 70),
        (DAY, 300, 190, 110),
    ]
    for date, total, five, one in points:
        await session.execute(
            text("""
                insert into wb.daily_snapshots (
                    nm_id, snapshot_date, rating, ratings_total,
                    ratings_5, ratings_4, ratings_3, ratings_2, ratings_1,
                    buyout_percent, orders_count, collected_at
                ) values (:nm, :date, 4.9, :total, :five, 0, 0, 0, :one,
                          87, 100, now())
            """),
            {"nm": NM_ID, "date": date, "total": total, "five": five, "one": one},
        )
    await session.execute(
        text("""
            update wb.signal_settings
               set baseline_days = 7, threshold = 10
             where signal_type = 'negative_spike'
        """)
    )
    clock = ManualClock(dt.datetime(2026, 9, 24, 9, 0, tzinfo=MOSCOW), MOSCOW)

    await analyze.run(session, clock, run_date=DAY)

    spike_count = (
        await session.execute(
            text("""
                select count(*) from wb.signals
                 where nm_id = :nm and signal_type = 'negative_spike'
            """),
            {"nm": NM_ID},
        )
    ).scalar_one()
    assert spike_count == 1


async def test_trial_signal_does_not_suppress_first_live_signal(session: AsyncSession) -> None:
    await session.execute(
        text("""
            insert into wb.products (nm_id, vendor_code, updated_at)
            values (:nm, 'TEST-TRIAL', now())
            on conflict (nm_id) do nothing
        """),
        {"nm": NM_ID},
    )
    repo = SignalRepository(session)
    now = dt.datetime(2026, 9, 24, 9, 0, tzinfo=MOSCOW)
    trial_id = await repo.add(
        SignalRow(
            nm_id=NM_ID,
            signal_type="rating_change",
            period_start=DAY - dt.timedelta(days=1),
            period_end=DAY - dt.timedelta(days=1),
            value_before=4.9,
            value_after=4.8,
            delta=-0.1,
            payload={},
        ),
        created_at=now - dt.timedelta(hours=1),
    )
    await repo.mark_dry_run(trial_id, observed_at=now)

    assert not await repo.is_suppressed(NM_ID, "rating_change", now=now, suppress_hours=24)
