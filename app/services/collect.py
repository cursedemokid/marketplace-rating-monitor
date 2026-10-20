"""Сбор данных из Wildberries. TSK-103, TSK-104, TSK-107.

Порядок шагов важен: сначала карточки, потом отзывы. Отзыв ссылается на
товар внешним ключом, и на артикул, которого нет в справочнике, его не
положить — а новые товары появляются у продавца постоянно.

Идемпотентность: повторный запуск за ту же дату обновляет записи, а не
создаёт вторые (FR-1.3). Проверяется тестом, а не на глаз.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass

import httpx
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.wb.client import (
    ITEM_RATING_BATCH,
    SALES_FUNNEL_BATCH,
    WbAuthError,
    WbClient,
    WbError,
)
from app.adapters.wb.models import Card, Feedback
from app.clock import Clock
from app.repositories.feedbacks import FeedbackRepository, FeedbackRow
from app.repositories.products import ProductRepository, ProductRow
from app.repositories.runs import RunRepository
from app.repositories.snapshots import RatingCounts, SnapshotRepository, SnapshotRow

UPSERT_CHUNK = 500
"""Сколько строк уходит в базу одним запросом. Больше — дольше держим транзакцию."""


BUYOUT_WINDOW_DAYS = 7
"""Согласованное окно расчёта выкупа — семь дней (решение G-08).

За один день на товаре с тремя заказами выкуп равен 33% или 100% без всякого
смысла, поэтому берём скользящее окно.
"""


@dataclass(frozen=True, slots=True)
class CollectResult:
    products: int
    feedbacks: int
    skipped_feedbacks: int
    """Отзывы на артикулы, которых нет в справочнике. Норма — ноль."""
    snapshots: int
    rechecked_dates: int
    failed_products: int = 0
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SnapshotBuildResult:
    written: int
    failed_nm_ids: frozenset[int]
    errors: tuple[str, ...]


WB_REQUEST_FAILURES = (WbError, httpx.HTTPError)


async def sync_products(
    session: AsyncSession,
    client: WbClient,
    *,
    commit_batches: bool = False,
    account_key: str = "default",
) -> int:
    """Активные и находящиеся в корзине карточки → wb.products. TSK-103."""
    repo = ProductRepository(session)
    batch: list[ProductRow] = []
    total = 0

    async def add(card: Card, *, monitored: bool) -> None:
        nonlocal total
        batch.append(
            ProductRow(
                nm_id=card.nm_id,
                vendor_code=card.vendor_code,
                brand=card.brand,
                subject_name=card.subject_name,
                size=card.size,
                title=card.title,
                account_key=account_key,
                is_monitored=monitored,
            )
        )
        if len(batch) >= UPSERT_CHUNK:
            total += await repo.upsert_many(batch)
            batch.clear()
            if commit_batches:
                await session.commit()

    try:
        async for card in client.iter_cards():
            await add(card, monitored=True)
        async for card in client.iter_trash_cards():
            await add(card, monitored=False)
    except WB_REQUEST_FAILURES:
        if batch:
            total += await repo.upsert_many(batch)
            if commit_batches:
                await session.commit()
        raise
    total += await repo.upsert_many(batch)
    if commit_batches:
        await session.commit()
    logger.info("Карточки: записано {}", total)
    return total


def to_row(feedback: Feedback, clock: Clock) -> FeedbackRow:
    """Отзыв Wildberries → строка базы.

    `created_date` считается в поясе заказчика: Wildberries отдаёт момент
    в UTC, а «день» у заказчика московский. Без приведения отзывы, пришедшие
    после 21:00 UTC, уедут на сутки назад, и дневные доли будут врать.
    """
    created_at = feedback.created_date
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=dt.UTC)
    return FeedbackRow(
        id=feedback.id,
        nm_id=feedback.nm_id,
        valuation=feedback.valuation,
        text=feedback.text,
        pros=feedback.pros,
        cons=feedback.cons,
        created_at=created_at,
        created_date=created_at.astimezone(clock.tz).date(),
    )


async def sync_feedbacks(
    session: AsyncSession,
    client: WbClient,
    clock: Clock,
    *,
    since: dt.datetime,
    commit_batches: bool = False,
    account_key: str = "default",
) -> tuple[int, int]:
    """Отзывы за окно → wb.feedbacks. TSK-104.

    Окно скользящее, а не «за вчера»: отзывы приходят с задержкой и
    редактируются, поэтому перечитываем последние дни целиком (FR-1.5).

    Возвращает (сохранено, пропущено).
    """
    repo = FeedbackRepository(session)
    known = await repo.known_nm_ids(account_key)
    batch: list[FeedbackRow] = []
    seen: set[str] = set()
    saved = 0
    skipped = 0

    async def flush() -> None:
        nonlocal saved
        saved += await repo.upsert_many(batch)
        batch.clear()
        if commit_batches:
            await session.commit()

    async def handle(feedback: Feedback) -> None:
        nonlocal skipped
        if feedback.id in seen:
            # Активный и архивный списки могут пересечься — второй раз
            # писать тот же отзыв незачем.
            return
        seen.add(feedback.id)
        if feedback.nm_id not in known:
            skipped += 1
            return
        batch.append(to_row(feedback, clock))
        if len(batch) >= UPSERT_CHUNK:
            await flush()

    try:
        async for feedback in client.iter_feedbacks(date_from=since):
            await handle(feedback)
        async for feedback in client.iter_archive_feedbacks(since=since):
            await handle(feedback)
    except WB_REQUEST_FAILURES:
        await flush()
        raise
    await flush()

    if skipped:
        logger.warning(
            "Пропущено отзывов на неизвестные артикулы: {}. "
            "Скорее всего, карточки не успели синхронизироваться",
            skipped,
        )
    logger.info("Отзывы: записано {}", saved)
    return saved, skipped


async def run(
    session: AsyncSession,
    client: WbClient,
    clock: Clock,
    *,
    run_date: dt.date,
    recheck_days: int,
    account_key: str = "default",
    analytics_available: bool = True,
) -> CollectResult:
    """Полный ночной прогон с журналом. TSK-107."""
    runs = RunRepository(session)
    run_id = await runs.start(run_date, clock.now(), account_key)
    await session.commit()

    since = dt.datetime.combine(
        run_date - dt.timedelta(days=recheck_days),
        dt.time.min,
        tzinfo=clock.tz,
    )
    errors: list[str] = []
    failed_nm_ids: set[int] = set()
    products = feedbacks = skipped = 0
    snapshots_written = 0
    try:
        try:
            products = await sync_products(
                session, client, commit_batches=True, account_key=account_key
            )
        except (WbAuthError, httpx.TransportError):
            raise
        except WB_REQUEST_FAILURES as exc:
            await session.rollback()
            errors.append(f"карточки: {exc}")
            logger.warning("Сбор карточек завершился частично: {}", exc)

        try:
            feedbacks, skipped = await sync_feedbacks(
                session, client, clock, since=since, commit_batches=True, account_key=account_key
            )
        except (WbAuthError, httpx.TransportError):
            raise
        except WB_REQUEST_FAILURES as exc:
            await session.rollback()
            errors.append(f"отзывы: {exc}")
            logger.warning("Сбор отзывов завершился частично: {}", exc)

        snapshots = await build_snapshots(
            session,
            client,
            on_date=run_date,
            account_key=account_key,
            analytics_available=analytics_available,
        )
        snapshots_written = snapshots.written
        failed_nm_ids.update(snapshots.failed_nm_ids)
        errors.extend(snapshots.errors)
        await session.commit()
        rechecked, recheck_failed, recheck_errors = await recheck_recent(
            session,
            client,
            on_date=run_date,
            days=recheck_days,
            commit_each=True,
            account_key=account_key,
            analytics_available=analytics_available,
        )
        failed_nm_ids.update(recheck_failed)
        errors.extend(recheck_errors)
    except (Exception, asyncio.CancelledError) as exc:
        await session.rollback()
        await runs.finish(
            run_id,
            finished_at=clock.now(),
            status="failed",
            products_ok=max(0, snapshots_written - len(failed_nm_ids)),
            products_failed=len(failed_nm_ids),
            error="; ".join(
                [*errors, "Прогон прерван" if isinstance(exc, asyncio.CancelledError) else str(exc)]
            )[:4000],
        )
        await session.commit()
        logger.error("Прогон за {} упал: {}", run_date, exc)
        raise

    await runs.finish(
        run_id,
        finished_at=clock.now(),
        status="partial" if skipped or errors else "ok",
        products_ok=max(0, snapshots.written - len(snapshots.failed_nm_ids)),
        products_failed=len(failed_nm_ids),
        error="; ".join(errors)[:4000] if errors else None,
    )
    await session.commit()
    return CollectResult(
        products=products,
        feedbacks=feedbacks,
        skipped_feedbacks=skipped,
        snapshots=snapshots.written,
        rechecked_dates=rechecked,
        failed_products=len(failed_nm_ids),
        errors=tuple(errors),
    )


def buyout_percent(orders: int, buyouts: int) -> float | None:
    """Процент выкупа с дробной частью.

    Своё деление, а не `conversions.buyoutPercent` из ответа Wildberries:
    там процент приходит ЦЕЛЫМ числом, а ТЗ п. 8 требует реагировать на
    падение в 0,2 процентного пункта. По целому проценту такое изменение
    не различить в принципе.
    """
    if orders <= 0:
        return None
    return round(buyouts / orders * 100, 2)


async def build_snapshots(
    session: AsyncSession,
    client: WbClient,
    *,
    on_date: dt.date,
    account_key: str | None = None,
    analytics_available: bool = True,
) -> SnapshotBuildResult:
    """Снимок показателей всех товаров на дату. TSK-105, TSK-106, TSK-107.

    Порядок: счётчики оценок считаем по своей базе отзывов, рейтинг берём
    из `item-rating`, заказы и выкупы — из воронки продаж.
    """
    products = ProductRepository(session)
    snapshots = SnapshotRepository(session)

    nm_ids = await products.monitored_nm_ids(account_key)
    if not nm_ids:
        logger.warning("Нет товаров под мониторингом — снимок пустой")
        return SnapshotBuildResult(0, frozenset(), ())

    counts = await snapshots.counts_as_of(on_date)

    rating_by_nm: dict[int, float] = {}
    rating_failed: set[int] = set()
    funnel_failed: set[int] = set()
    errors: list[str] = []
    if not analytics_available:
        rating_failed.update(nm_ids)
        funnel_failed.update(nm_ids)
        errors.append(f"{account_key or 'default'}: категория Аналитика недоступна")
    for offset in range(0, len(nm_ids) if analytics_available else 0, ITEM_RATING_BATCH):
        batch = nm_ids[offset : offset + ITEM_RATING_BATCH]
        try:
            ratings = await client.fetch_item_rating(
                batch, period_start=on_date, period_end=on_date
            )
        except (WbAuthError, httpx.TransportError):
            raise
        except WB_REQUEST_FAILURES as exc:
            rating_failed.update(batch)
            errors.append(f"{on_date} рейтинг {batch[0]}…{batch[-1]}: {exc}")
            logger.warning("Ошибка рейтинга для {} артикулов за {}: {}", len(batch), on_date, exc)
        else:
            rating_by_nm.update({item.nm_id: item.feedback_rating.current for item in ratings})

    funnel_start = on_date - dt.timedelta(days=BUYOUT_WINDOW_DAYS - 1)
    funnel_by_nm = {}
    for offset in range(0, len(nm_ids) if analytics_available else 0, SALES_FUNNEL_BATCH):
        batch = nm_ids[offset : offset + SALES_FUNNEL_BATCH]
        try:
            funnel = await client.fetch_sales_funnel(
                batch, period_start=funnel_start, period_end=on_date
            )
        except (WbAuthError, httpx.TransportError):
            raise
        except WB_REQUEST_FAILURES as exc:
            funnel_failed.update(batch)
            errors.append(f"{on_date} выкуп {batch[0]}…{batch[-1]}: {exc}")
            logger.warning("Ошибка выкупа для {} артикулов за {}: {}", len(batch), on_date, exc)
        else:
            funnel_by_nm.update({row.product.nm_id: row.statistic.selected for row in funnel})

    rows: list[SnapshotRow] = []
    empty = RatingCounts(total=0, five=0, four=0, three=0, two=0, one=0)
    for nm_id in nm_ids:
        marks = counts.get(nm_id, empty)
        stat = funnel_by_nm.get(nm_id)
        orders = stat.order_count if stat else None
        buyouts = stat.buyout_count if stat else None
        rows.append(
            SnapshotRow(
                nm_id=nm_id,
                snapshot_date=on_date,
                rating=rating_by_nm.get(nm_id),
                ratings_total=marks.total,
                ratings_5=marks.five,
                ratings_4=marks.four,
                ratings_3=marks.three,
                ratings_2=marks.two,
                ratings_1=marks.one,
                buyout_percent=(
                    buyout_percent(orders, buyouts)
                    if orders is not None and buyouts is not None
                    else None
                ),
                orders_count=orders,
                buyouts_count=buyouts,
                rating_complete=nm_id not in rating_failed,
                funnel_complete=nm_id not in funnel_failed,
            )
        )

    written = await snapshots.upsert_many(rows)
    logger.info("Снимок за {}: записано строк {}", on_date, written)
    return SnapshotBuildResult(written, frozenset(rating_failed | funnel_failed), tuple(errors))


async def recheck_recent(
    session: AsyncSession,
    client: WbClient,
    *,
    on_date: dt.date,
    days: int,
    commit_each: bool = False,
    account_key: str | None = None,
    analytics_available: bool = True,
) -> tuple[int, frozenset[int], tuple[str, ...]]:
    """Перепересчёт снимков за последние дни. FR-1.5.

    Аналитика Wildberries отстаёт на несколько часов и досчитывается задним
    числом: возврат, пришедший сегодня, WB отнесёт к дате заказа. Значит
    вчерашний снимок завтра может измениться, и его надо перезаписывать,
    а не только дописывать новый.
    """
    rechecked = 0
    failed_nm_ids: set[int] = set()
    errors: list[str] = []
    for shift in range(1, days + 1):
        target = on_date - dt.timedelta(days=shift)
        result = await build_snapshots(
            session,
            client,
            on_date=target,
            account_key=account_key,
            analytics_available=analytics_available,
        )
        failed_nm_ids.update(result.failed_nm_ids)
        errors.extend(result.errors)
        if commit_each:
            await session.commit()
        rechecked += 1
    logger.info("Перепроверено прошлых дней: {}", rechecked)
    return rechecked, frozenset(failed_nm_ids), tuple(errors)
