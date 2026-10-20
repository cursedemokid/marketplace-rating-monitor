"""Проверки состояния доставки на настоящей PostgreSQL."""

from __future__ import annotations

import datetime as dt

from app.adapters.messenger.base import DeliveryResult
from app.clock import ManualClock
from app.repositories.products import ProductRepository, ProductRow
from app.repositories.signals import SignalRepository, SignalRow
from app.services import notify
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

NM_ID = 999_000_006


async def _pending_signal(session: AsyncSession, clock: ManualClock) -> int:
    await ProductRepository(session).upsert_many(
        [
            ProductRow(
                nm_id=NM_ID,
                vendor_code="TEST-NOTIFY",
                brand=None,
                subject_name=None,
                size=None,
            )
        ]
    )
    return await SignalRepository(session).add(
        SignalRow(
            nm_id=NM_ID,
            signal_type="rating_change",
            period_start=dt.date(2026, 9, 23),
            period_end=dt.date(2026, 9, 23),
            value_before=4.9,
            value_after=4.8,
            delta=-0.1,
            payload={},
        ),
        created_at=clock.now(),
    )


async def test_dry_run_does_not_mark_signal_sent_or_queue_it_for_live_delivery(
    session: AsyncSession,
    clock: ManualClock,
) -> None:
    signal_id = await _pending_signal(session, clock)

    await notify.run(session, clock, None, dry_run=True)

    state = (
        await session.execute(
            text("select sent_at, dry_run_at from wb.signals where id = :id"),
            {"id": signal_id},
        )
    ).one()
    assert state.sent_at is None
    assert state.dry_run_at == clock.now()
    assert all(row.id != signal_id for row in await SignalRepository(session).outbox())


class _RecordingChannel:
    name = "test"

    def __init__(self) -> None:
        self.payload_ids: list[str] = []

    async def send(self, text: str, *, payload_id: str = "") -> DeliveryResult:
        self.payload_ids.append(payload_id)
        return DeliveryResult(ok=True, message_id=len(self.payload_ids))


class _FailingChannel:
    name = "test_failure"

    async def send(self, text: str, *, payload_id: str = "") -> DeliveryResult:
        return DeliveryResult(ok=False, error="канал недоступен")


async def test_notify_processes_more_than_one_outbox_page(
    session: AsyncSession,
    clock: ManualClock,
) -> None:
    signal_ids = [await _pending_signal(session, clock) for _ in range(101)]
    channel = _RecordingChannel()

    result = await notify.run(session, clock, channel, limit=100)

    assert result.sent == 101
    assert len(channel.payload_ids) == 101
    remaining = await SignalRepository(session).outbox()
    assert not any(row.id in signal_ids for row in remaining)


async def test_failed_delivery_stays_in_outbox_for_retry(
    session: AsyncSession,
    clock: ManualClock,
) -> None:
    signal_ids = [await _pending_signal(session, clock) for _ in range(2)]

    result = await notify.run(session, clock, _FailingChannel(), limit=1)

    assert result.failed == 2
    rows = (
        await session.execute(
            text("select id, sent_at, send_error from wb.signals where id = any(:ids)"),
            {"ids": signal_ids},
        )
    ).all()
    assert len(rows) == 2
    assert all(row.sent_at is None and row.send_error == "канал недоступен" for row in rows)
    assert {row.id for row in await SignalRepository(session).outbox()} >= set(signal_ids)


async def test_unsent_negative_signal_gets_new_reason_before_delivery(
    session: AsyncSession,
    clock: ManualClock,
) -> None:
    await _pending_signal(session, clock)  # ensures the product exists
    repo = SignalRepository(session)
    signal_id = await repo.add(
        SignalRow(
            nm_id=NM_ID,
            signal_type="negative_spike",
            period_start=dt.date(2026, 9, 23),
            period_end=dt.date(2026, 9, 23),
            value_before=2,
            value_after=4,
            delta=2,
            payload={},
        ),
        created_at=clock.now(),
    )
    await session.execute(
        text("""
            insert into wb.feedbacks
                (id, nm_id, valuation, created_at, created_date, imported_at)
            values ('notify-reason-1', :nm_id, 1, now(), 'DemoPhone', now())
        """),
        {"nm_id": NM_ID},
    )
    await session.execute(
        text("""
            insert into wb.feedback_reasons
                (feedback_id, reason_id, model, classified_at)
            select 'notify-reason-1', id, 'test', now()
              from wb.negative_reasons where name = 'протекают'
        """)
    )

    await repo.refresh_unsent_summaries()

    summary = await session.scalar(
        text("select ai_summary from wb.signals where id = :id"), {"id": signal_id}
    )
    assert summary == "протекают"


async def test_refresh_unsent_summaries_excludes_external_reason_and_preserves_sent(
    session: AsyncSession,
    clock: ManualClock,
) -> None:
    pvz_nm = NM_ID + 1
    mixed_nm = NM_ID + 2
    products = [
        ProductRow(
            nm_id=nm_id, vendor_code=f"TEST-PVZ-{nm_id}", brand=None, subject_name=None, size=None
        )
        for nm_id in (pvz_nm, mixed_nm)
    ]
    await ProductRepository(session).upsert_many(products)
    repo = SignalRepository(session)

    async def add_signal(nm_id: int) -> int:
        return await repo.add(
            SignalRow(
                nm_id=nm_id,
                signal_type="negative_spike",
                period_start=dt.date(2026, 9, 23),
                period_end=dt.date(2026, 9, 23),
                value_before=2,
                value_after=4,
                delta=2,
                payload={},
            ),
            created_at=clock.now(),
        )

    pvz_only_signal = await add_signal(pvz_nm)
    mixed_signal = await add_signal(mixed_nm)
    sent_signal = await add_signal(mixed_nm)
    await session.execute(
        text("""
            insert into wb.feedbacks
                (id, nm_id, valuation, created_at, created_date, imported_at)
            values ('notify-pvz-only', :pvz_nm, 1, now(), 'DemoPhone', now()),
                   ('notify-mixed', :mixed_nm, 2, now(), 'DemoPhone', now()),
                   ('notify-mixed-pvz-1', :mixed_nm, 1, now(), 'DemoPhone', now()),
                   ('notify-mixed-pvz-2', :mixed_nm, 1, now(), 'DemoPhone', now())
        """),
        {"pvz_nm": pvz_nm, "mixed_nm": mixed_nm},
    )
    await session.execute(
        text("""
            insert into wb.feedback_reasons (feedback_id, reason_id, model, classified_at)
            select f.id, r.id, 'test', now()
              from (values ('notify-pvz-only'), ('notify-mixed'),
                           ('notify-mixed-pvz-1'), ('notify-mixed-pvz-2')) f(id)
              join wb.negative_reasons r on r.name = 'работа ПВЗ'
            union all
            select 'notify-mixed', r.id, 'test', now()
              from wb.negative_reasons r where r.name = 'протекают'
        """)
    )
    await session.execute(
        text(
            "update wb.signals set ai_summary = 'предыдущая сводка', sent_at = :sent where id = :id"
        ),
        {"id": sent_signal, "sent": clock.now()},
    )

    await repo.refresh_unsent_summaries()

    rows = await session.execute(
        text("select id, ai_summary from wb.signals where id = any(:ids)"),
        {"ids": [pvz_only_signal, mixed_signal, sent_signal]},
    )
    summaries = {row.id: row.ai_summary for row in rows.all()}
    assert summaries[pvz_only_signal] is None
    assert summaries[mixed_signal] == "протекают"
    assert summaries[sent_signal] == "предыдущая сводка"
