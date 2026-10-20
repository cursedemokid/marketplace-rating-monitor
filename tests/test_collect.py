"""Сбор: приведение времени и идемпотентность записи.

Интеграционная часть работает на живой базе из `docker compose`. Если базы
нет — тесты пропускаются, а не падают: контрактные тесты должны проходить
и на машине без докера.

Ничего не коммитим: транзакция откатывается, база остаётся как была.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import zoneinfo
from collections.abc import AsyncIterator

import httpx
import pytest
from app.adapters.wb.client import WbAuthError, WbError
from app.adapters.wb.models import Card, Feedback
from app.clock import ManualClock
from app.db.engine import build_engine
from app.repositories.feedbacks import FeedbackRepository
from app.repositories.products import ProductRepository, ProductRow
from app.services.collect import run, sync_feedbacks, sync_products, to_row
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MOSCOW = zoneinfo.ZoneInfo("Europe/Moscow")


def make_feedback(created: str, nm_id: int = 1, fid: str = "fb-1") -> Feedback:
    return Feedback.model_validate(
        {
            "id": fid,
            "text": "протекает",
            "pros": "",
            "cons": "",
            "productValuation": 2,
            "createdDate": created,
            "productDetails": {"nmId": nm_id},
        }
    )


def test_late_evening_utc_belongs_to_next_moscow_day() -> None:
    """21:30 UTC 23 сентября — это уже 24 сентября по Москве.

    Без приведения к поясу заказчика такой отзыв попадёт во вчерашний день,
    и дневные доли оценок будут врать. NFR-8.
    """
    clock = ManualClock(dt.datetime(2026, 9, 24, 9, 0, tzinfo=MOSCOW), MOSCOW)
    row = to_row(make_feedback("DemoPhoneT21:30:00Z"), clock)
    assert row.created_date == dt.date(2026, 9, 24)


def test_morning_utc_stays_in_same_moscow_day() -> None:
    clock = ManualClock(dt.datetime(2026, 9, 24, 9, 0, tzinfo=MOSCOW), MOSCOW)
    row = to_row(make_feedback("DemoPhoneT08:00:00Z"), clock)
    assert row.created_date == dt.date(2026, 9, 23)


PRODUCT = ProductRow(
    nm_id=999_000_001,
    vendor_code="TEST-0001",
    brand="TEST",
    subject_name="Подгузники",
    size="M",
    title="Ночные подгузники",
)


class _ActiveAndTrashedCards:
    async def iter_cards(self) -> AsyncIterator[Card]:
        yield Card.model_validate({"nmID": 999_000_111, "vendorCode": "ACTIVE-111"})

    async def iter_trash_cards(self) -> AsyncIterator[Card]:
        yield Card.model_validate({"nmID": 999_000_112, "vendorCode": "TRASH-112"})

    async def iter_feedbacks(self, *, date_from: dt.datetime) -> AsyncIterator[Feedback]:
        yield make_feedback("DemoPhoneT10:00:00Z", nm_id=999_000_112, fid="trash-fb")

    async def iter_archive_feedbacks(self, *, since: dt.datetime) -> AsyncIterator[Feedback]:
        if False:
            yield make_feedback("DemoPhoneT10:00:00Z")


async def test_trash_card_feedback_is_saved_without_monitoring_deleted_product(
    session: AsyncSession,
) -> None:
    client = _ActiveAndTrashedCards()
    clock = ManualClock(dt.datetime(2026, 9, 24, 9, 0, tzinfo=MOSCOW), MOSCOW)

    await sync_products(session, client)
    saved, skipped = await sync_feedbacks(
        session, client, clock, since=dt.datetime(2026, 9, 23, tzinfo=dt.UTC)
    )
    products = await session.execute(
        text(
            "select nm_id, is_monitored from wb.products "
            "where nm_id in (4429491149, 5074281725) order by nm_id"
        )
    )

    assert products.all() == [(999_000_111, True), (999_000_112, False)]
    assert (saved, skipped) == (1, 0)


async def test_products_upsert_is_idempotent(session: AsyncSession) -> None:
    """Повторный прогон обновляет строку, а не создаёт вторую. TSK-103."""
    repo = ProductRepository(session)
    before = await repo.count()

    await repo.upsert_many([PRODUCT])
    after_first = await repo.count()
    await repo.upsert_many([PRODUCT])
    after_second = await repo.count()

    assert after_first == before + 1
    assert after_second == after_first, "второй прогон не должен плодить дубли"


async def test_products_upsert_updates_fields(session: AsyncSession) -> None:
    repo = ProductRepository(session)
    await repo.upsert_many([PRODUCT])
    renamed = ProductRow(
        nm_id=PRODUCT.nm_id,
        vendor_code="TEST-0001",
        brand="DEMO",
        subject_name=PRODUCT.subject_name,
        size="L",
        title="Дневные подгузники",
    )
    await repo.upsert_many([renamed])

    result = await session.execute(
        text("select brand, size, title from wb.products where nm_id = :id"),
        {"id": PRODUCT.nm_id},
    )
    assert result.one() == ("DEMO", "L", "Дневные подгузники")


async def test_feedbacks_upsert_is_idempotent(session: AsyncSession) -> None:
    """Первичный ключ — id от Wildberries, повтор не дублирует. TSK-104."""
    await ProductRepository(session).upsert_many([PRODUCT])
    repo = FeedbackRepository(session)
    clock = ManualClock(dt.datetime(2026, 9, 24, 9, 0, tzinfo=MOSCOW), MOSCOW)
    row = to_row(make_feedback("DemoPhoneT10:00:00Z", nm_id=PRODUCT.nm_id, fid="test-fb"), clock)

    before = await repo.count()
    await repo.upsert_many([row])
    await repo.upsert_many([row])

    assert await repo.count() == before + 1


@pytest.fixture
async def committing_session() -> AsyncIterator[AsyncSession]:
    """Коммиты сервиса остаются внутри внешней тестовой транзакции."""
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("нет DATABASE_URL")
    engine = build_engine(url)
    try:
        async with engine.connect() as conn:
            outer = await conn.begin()
            try:
                async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as db:
                    yield db
            finally:
                await outer.rollback()
    finally:
        await engine.dispose()


class _FailingAfterFeedbacks:
    def __init__(self, *, auth: bool) -> None:
        self.auth = auth

    async def iter_cards(self) -> AsyncIterator[Card]:
        yield Card.model_validate({"nmID": 999_000_101, "vendorCode": "LATE-FAIL"})

    async def iter_trash_cards(self) -> AsyncIterator[Card]:
        if False:
            yield Card.model_validate({"nmID": 999_000_102, "vendorCode": "TRASH"})

    async def iter_feedbacks(self, *, date_from: dt.datetime) -> AsyncIterator[Feedback]:
        yield make_feedback("DemoPhoneT10:00:00Z", nm_id=999_000_101, fid="late-fb")

    async def iter_archive_feedbacks(self, *, since: dt.datetime) -> AsyncIterator[Feedback]:
        for _ in ():
            yield _

    async def fetch_item_rating(
        self, nm_ids: list[int], *, period_start: dt.date, period_end: dt.date
    ) -> None:
        if self.auth:
            raise WbAuthError("token rejected")
        raise WbError("rating unavailable")

    async def fetch_sales_funnel(
        self, nm_ids: list[int], *, period_start: dt.date, period_end: dt.date
    ) -> list[object]:
        return []


async def _collected_counts(db: AsyncSession) -> tuple[int, int]:
    products = (
        await db.execute(text("select count(*) from wb.products where nm_id = 2230490824"))
    ).scalar_one()
    feedbacks = (
        await db.execute(text("select count(*) from wb.feedbacks where id = 'late-fb'"))
    ).scalar_one()
    return products, feedbacks


async def test_recoverable_analytics_failure_keeps_collected_data_and_marks_partial(
    committing_session: AsyncSession,
    clock: ManualClock,
) -> None:
    db = committing_session
    result = await run(
        db, _FailingAfterFeedbacks(auth=False), clock, run_date=clock.today(), recheck_days=0
    )

    assert result.failed_products == 1
    assert result.products == 1 and result.feedbacks == 1 and result.snapshots == 1
    status = (
        await db.execute(
            text("select status, products_failed from wb.collection_runs order by id desc limit 1")
        )
    ).one()
    assert status == ("partial", 1)
    assert await _collected_counts(db) == (1, 1)


async def test_auth_failure_keeps_completed_stages_and_marks_failed(
    committing_session: AsyncSession,
    clock: ManualClock,
) -> None:
    db = committing_session
    with pytest.raises(WbAuthError):
        await run(
            db, _FailingAfterFeedbacks(auth=True), clock, run_date=clock.today(), recheck_days=0
        )

    status = (
        await db.execute(text("select status from wb.collection_runs order by id desc limit 1"))
    ).scalar_one()
    assert status == "failed"
    assert await _collected_counts(db) == (1, 1)


class _DisconnectedAfterFeedbacks(_FailingAfterFeedbacks):
    async def fetch_item_rating(
        self, nm_ids: list[int], *, period_start: dt.date, period_end: dt.date
    ) -> None:
        raise httpx.ConnectError("dns unavailable")


async def test_network_outage_stops_collection_after_retries(
    committing_session: AsyncSession, clock: ManualClock
) -> None:
    db = committing_session
    with pytest.raises(httpx.ConnectError):
        await run(
            db,
            _DisconnectedAfterFeedbacks(auth=False),
            clock,
            run_date=clock.today(),
            recheck_days=0,
        )

    status = (
        await db.execute(text("select status from wb.collection_runs order by id desc limit 1"))
    ).scalar_one()
    assert status == "failed"
    assert await _collected_counts(db) == (1, 1)


class _CancelledAfterFeedbacks(_FailingAfterFeedbacks):
    async def fetch_item_rating(
        self, nm_ids: list[int], *, period_start: dt.date, period_end: dt.date
    ) -> None:
        raise asyncio.CancelledError


async def test_cancelled_collection_marks_run_failed(
    committing_session: AsyncSession, clock: ManualClock
) -> None:
    db = committing_session
    with pytest.raises(asyncio.CancelledError):
        await run(
            db,
            _CancelledAfterFeedbacks(auth=False),
            clock,
            run_date=clock.today(),
            recheck_days=0,
        )

    status = (
        await db.execute(
            text(
                "select status, finished_at, error from wb.collection_runs order by id desc limit 1"
            )
        )
    ).one()
    assert status.status == "failed"
    assert status.finished_at is not None
    assert "прерван" in status.error.lower()


class _BrokenCards(_FailingAfterFeedbacks):
    async def iter_cards(self) -> AsyncIterator[Card]:
        yield Card.model_validate({"nmID": 999_000_101, "vendorCode": "LATE-FAIL"})
        raise WbError("card page unavailable")

    async def fetch_item_rating(
        self, nm_ids: list[int], *, period_start: dt.date, period_end: dt.date
    ) -> list[object]:
        return []


async def test_card_page_failure_keeps_cards_already_received(
    committing_session: AsyncSession,
    clock: ManualClock,
) -> None:
    db = committing_session
    result = await run(db, _BrokenCards(auth=False), clock, run_date=clock.today(), recheck_days=0)

    assert result.snapshots == 1
    status = (
        await db.execute(
            text("select status, error from wb.collection_runs order by id desc limit 1")
        )
    ).one()
    assert status.status == "partial" and "card page unavailable" in status.error
    assert (await _collected_counts(db))[0] == 1
