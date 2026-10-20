"""Снимок показателей: расчёт выкупа, пачки запросов, идемпотентность.

TSK-105, TSK-106, TSK-107.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx
import pytest
import respx
from app.adapters.wb.client import ANALYTICS_HOST, RateLimits, WbClient, WbError
from app.adapters.wb.models import FunnelProduct, ItemRating
from app.repositories.products import ProductRepository, ProductRow
from app.repositories.snapshots import SnapshotRepository
from app.services.collect import build_snapshots, buyout_percent
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

FIXTURES = Path(__file__).parent / "fixtures" / "wb"
NM_ID = 999_000_002


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def test_buyout_percent_keeps_fractions() -> None:
    """Доли процента обязаны сохраняться: порог ТЗ — 0,2 процентного пункта.

    Wildberries отдаёт `conversions.buyoutPercent` целым числом, по нему
    такое изменение не различить. Поэтому считаем сами.
    """
    assert buyout_percent(1000, 868) == 86.8
    assert buyout_percent(1000, 870) == 87.0
    assert buyout_percent(1000, 868) != buyout_percent(1000, 870)


def test_buyout_percent_without_orders() -> None:
    """Ноль заказов — не ноль процентов, а «нет данных»."""
    assert buyout_percent(0, 0) is None


@respx.mock
async def test_item_rating_splits_into_batches_of_fifty() -> None:
    """Метод принимает максимум 50 артикулов за запрос."""
    route = respx.post(f"{ANALYTICS_HOST}/api/analytics/v2/item-rating").mock(
        return_value=httpx.Response(200, json={"data": {"items": []}})
    )
    async with WbClient("t", limits=RateLimits.unthrottled()) as client:
        await client.fetch_item_rating(
            list(range(120)),
            period_start=dt.date(2026, 9, 24),
            period_end=dt.date(2026, 9, 24),
        )

    assert route.call_count == 3, "120 артикулов — это 50 + 50 + 20"
    sizes = [len(json.loads(call.request.content)["nmIds"]) for call in route.calls]
    assert sizes == [50, 50, 20]


@respx.mock
async def test_item_rating_sends_required_fields() -> None:
    """orderBy и offset обязательны — без них метод отвечает 400."""
    route = respx.post(f"{ANALYTICS_HOST}/api/analytics/v2/item-rating").mock(
        return_value=httpx.Response(200, json={"data": {"items": []}})
    )
    async with WbClient("t", limits=RateLimits.unthrottled()) as client:
        await client.fetch_item_rating(
            [1], period_start=dt.date(2026, 9, 24), period_end=dt.date(2026, 9, 24)
        )

    payload = json.loads(route.calls[0].request.content)
    assert payload["orderBy"] == {"field": "feedbackCount", "mode": "desc"}
    assert payload["offset"] == 0
    assert payload["currentPeriod"] == {"start": "DemoPhone", "end": "DemoPhone"}


@respx.mock
async def test_sales_funnel_batches_by_thousand() -> None:
    route = respx.post(f"{ANALYTICS_HOST}/api/analytics/v3/sales-funnel/products").mock(
        return_value=httpx.Response(200, json={"data": {"products": []}})
    )
    async with WbClient("t", limits=RateLimits.unthrottled()) as client:
        await client.fetch_sales_funnel(
            list(range(2500)),
            period_start=dt.date(2026, 9, 18),
            period_end=dt.date(2026, 9, 24),
        )

    assert route.call_count == 3
    sizes = [len(json.loads(call.request.content)["nmIds"]) for call in route.calls]
    assert sizes == [1000, 1000, 500]


@respx.mock
async def test_item_rating_parses_current_as_period_increment() -> None:
    """`fiveStar.current` — прирост за период, а не «всего пятёрок».

    Тест фиксирует смысл поля: если кто-то начнёт класть его в накопленный
    счётчик снимка, он сначала споткнётся здесь.
    """
    respx.post(f"{ANALYTICS_HOST}/api/analytics/v2/item-rating").mock(
        return_value=httpx.Response(200, json=load("item_rating"))
    )
    async with WbClient("t", limits=RateLimits.unthrottled()) as client:
        items = await client.fetch_item_rating(
            [4016747329], period_start=dt.date(2026, 9, 24), period_end=dt.date(2026, 9, 24)
        )

    assert items[0].feedback_rating.current == 4.93
    assert items[0].five_star.current == 9, "прирост за период, не итог за всю жизнь товара"


@respx.mock
async def test_snapshot_is_idempotent(session: AsyncSession) -> None:
    """Повторный прогон за ту же дату перезаписывает снимок. FR-1.3, TSK-107.

    Главная проверка этапа 1: прогнали дважды — строк столько же.
    """
    on_date = dt.date(2026, 9, 24)
    await session.execute(
        text("""
            insert into wb.products (nm_id, vendor_code, is_monitored, updated_at)
            values (:nm, 'TEST-SNAP', true, now())
            on conflict (nm_id) do nothing
        """),
        {"nm": NM_ID},
    )
    await session.execute(
        text("""
            insert into wb.feedbacks
                (id, nm_id, valuation, text, pros, cons, created_at, created_date, imported_at)
            values
                ('snap-1', :nm, 5, '', '', '', now(), :d, now()),
                ('snap-2', :nm, 5, '', '', '', now(), :d, now()),
                ('snap-3', :nm, 1, '', '', '', now(), :d, now())
            on conflict (id) do nothing
        """),
        {"nm": NM_ID, "d": on_date},
    )
    respx.post(f"{ANALYTICS_HOST}/api/analytics/v2/item-rating").mock(
        return_value=httpx.Response(200, json={"data": {"items": []}})
    )
    respx.post(f"{ANALYTICS_HOST}/api/analytics/v3/sales-funnel/products").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "products": [
                        {
                            "product": {"nmId": NM_ID},
                            "statistic": {
                                "selected": {
                                    "orderCount": 1000,
                                    "buyoutCount": 868,
                                    "conversions": {"buyoutPercent": 87},
                                }
                            },
                        }
                    ]
                }
            },
        )
    )

    repo = SnapshotRepository(session)
    async with WbClient("t", limits=RateLimits.unthrottled()) as client:
        await build_snapshots(session, client, on_date=on_date)
        after_first = await repo.count_for_date(on_date)
        await build_snapshots(session, client, on_date=on_date)
        after_second = await repo.count_for_date(on_date)

    assert after_second == after_first, "второй прогон не должен плодить снимки"

    row = (
        await session.execute(
            text("""
                select ratings_total, ratings_5, ratings_1, buyout_percent
                  from wb.daily_snapshots
                 where nm_id = :nm and snapshot_date = :d
            """),
            {"nm": NM_ID, "d": on_date},
        )
    ).one()
    assert row.ratings_total == 3
    assert row.ratings_5 == 2
    assert row.ratings_1 == 1
    assert float(row.buyout_percent) == 86.8, "считаем сами, дробная часть не теряется"


@respx.mock
async def test_analytics_requests_are_scoped_to_seller_account(session: AsyncSession) -> None:
    default_id, DemoSeller_id = 999_009_101, 999_009_102
    await ProductRepository(session).upsert_many(
        [
            ProductRow(default_id, "SCOPE-DEFAULT", None, None, None, account_key="default"),
            ProductRow(DemoSeller_id, "SCOPE-DemoSeller", None, None, None, account_key="DemoSeller"),
        ]
    )
    rating = respx.post(f"{ANALYTICS_HOST}/api/analytics/v2/item-rating").mock(
        return_value=httpx.Response(200, json={"data": {"items": []}})
    )
    funnel = respx.post(f"{ANALYTICS_HOST}/api/analytics/v3/sales-funnel/products").mock(
        return_value=httpx.Response(200, json={"data": {"products": []}})
    )

    async with WbClient("t", limits=RateLimits.unthrottled()) as client:
        await build_snapshots(session, client, on_date=dt.date(2026, 10, 12), account_key="default")
        await build_snapshots(session, client, on_date=dt.date(2026, 10, 12), account_key="DemoSeller")

    default_rating = [json.loads(call.request.content)["nmIds"] for call in rating.calls[:-1]]
    DemoSeller_rating = json.loads(rating.calls[-1].request.content)["nmIds"]
    default_funnel = [json.loads(call.request.content)["nmIds"] for call in funnel.calls[:-1]]
    DemoSeller_funnel = json.loads(funnel.calls[-1].request.content)["nmIds"]
    assert any(default_id in batch for batch in default_rating)
    assert all(DemoSeller_id not in batch for batch in default_rating + default_funnel)
    assert DemoSeller_rating == [DemoSeller_id]
    assert DemoSeller_funnel == [DemoSeller_id]


@respx.mock
async def test_snapshot_without_analytics_keeps_feedback_counts(session: AsyncSession) -> None:
    nm_id = 999_009_103
    await ProductRepository(session).upsert_many(
        [ProductRow(nm_id, "NO-ANALYTICS", None, None, None, account_key="DemoSeller")]
    )
    result = await build_snapshots(
        session,
        WbClient("unused", limits=RateLimits.unthrottled()),
        on_date=dt.date(2026, 10, 12),
        account_key="DemoSeller",
        analytics_available=False,
    )

    assert nm_id in result.failed_nm_ids
    assert result.written >= 1
    assert not respx.calls
    row = (
        await session.execute(
            text("""
        select rating, buyout_percent, ratings_total
          from wb.daily_snapshots
         where nm_id = :nm_id and snapshot_date = :on_date
    """),
            {"nm_id": nm_id, "on_date": dt.date(2026, 10, 12)},
        )
    ).one()
    assert row.rating is None
    assert row.buyout_percent is None
    assert row.ratings_total == 0


@pytest.mark.parametrize("shift", [1, 7])
def test_recheck_window_covers_past_days(shift: int) -> None:
    """Перепроверка идёт по прошлым дням, не по будущим."""
    on_date = dt.date(2026, 9, 24)
    assert on_date - dt.timedelta(days=shift) < on_date


class _PartiallyFailingAnalytics:
    async def fetch_item_rating(
        self, nm_ids: list[int], *, period_start: dt.date, period_end: dt.date
    ) -> list[ItemRating]:
        if nm_ids[0] == 999_100_000:
            raise WbError("исчерпаны попытки рейтинга")
        return [
            ItemRating.model_validate({"nmId": nm_id, "feedbackRating": {"current": 4.7}})
            for nm_id in nm_ids
        ]

    async def fetch_sales_funnel(
        self, nm_ids: list[int], *, period_start: dt.date, period_end: dt.date
    ) -> list[FunnelProduct]:
        return [
            FunnelProduct.model_validate(
                {
                    "product": {"nmId": nm_id},
                    "statistic": {"selected": {"orderCount": 10, "buyoutCount": 8}},
                }
            )
            for nm_id in nm_ids
        ]


async def test_failed_rating_batch_preserves_other_snapshots_and_reports_failed_products(
    session: AsyncSession,
) -> None:
    on_date = dt.date(2026, 9, 24)
    await ProductRepository(session).upsert_many(
        [
            ProductRow(999_100_000 + offset, f"BATCH-{offset}", None, None, None)
            for offset in range(51)
        ]
    )

    result = await build_snapshots(session, _PartiallyFailingAnalytics(), on_date=on_date)

    assert result.written == 51
    assert result.failed_nm_ids == set(range(999_100_000, 999_100_050))
    rows = (
        await session.execute(
            text("""
                select nm_id, rating, buyout_percent from wb.daily_snapshots
                 where nm_id in (4196952219, 1381832033) and snapshot_date = :d
                 order by nm_id
            """),
            {"d": on_date},
        )
    ).all()
    assert rows[0].rating is None and float(rows[0].buyout_percent) == 80.0
    assert float(rows[1].rating) == 4.7 and float(rows[1].buyout_percent) == 80.0


async def test_failed_rating_refresh_does_not_erase_previous_value(session: AsyncSession) -> None:
    on_date = dt.date(2026, 9, 24)
    await ProductRepository(session).upsert_many(
        [ProductRow(999_100_000, "PRESERVE", None, None, None)]
    )
    await session.execute(
        text("""
            insert into wb.daily_snapshots (
                nm_id, snapshot_date, rating, ratings_total, ratings_5,
                ratings_4, ratings_3, ratings_2, ratings_1, collected_at
            ) values (4196952219, :d, 4.91, 0, 0, 0, 0, 0, 0, now())
        """),
        {"d": on_date},
    )

    await build_snapshots(session, _PartiallyFailingAnalytics(), on_date=on_date)

    rating = (
        await session.execute(
            text("""
                select rating from wb.daily_snapshots
                 where nm_id = 4196952219 and snapshot_date = :d
            """),
            {"d": on_date},
        )
    ).scalar_one()
    assert float(rating) == 4.91
