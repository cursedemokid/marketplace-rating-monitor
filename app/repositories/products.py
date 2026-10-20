"""Справочник товаров: карточки Wildberries. TSK-103."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class ProductRow:
    nm_id: int
    vendor_code: str
    brand: str | None
    subject_name: str | None
    size: str | None
    title: str | None = None
    account_key: str = "default"
    is_monitored: bool = True


_UPSERT = text("""
    insert into wb.products (
        nm_id, vendor_code, brand, subject_name, size, title, account_key, is_monitored, updated_at
    )
    values (:nm_id, :vendor_code, :brand, :subject_name, :size, :title,
            :account_key, :is_monitored, now())
    on conflict (nm_id) do update set
        vendor_code  = excluded.vendor_code,
        brand        = coalesce(excluded.brand, wb.products.brand),
        subject_name = coalesce(excluded.subject_name, wb.products.subject_name),
        size         = coalesce(excluded.size, wb.products.size),
        title        = coalesce(excluded.title, wb.products.title),
        account_key  = excluded.account_key,
        is_monitored = excluded.is_monitored or wb.products.monitoring_added_manually,
        updated_at   = now()
""")


class ProductRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_many(self, rows: Sequence[ProductRow]) -> int:
        """Повторный запуск обновляет, а не дублирует — требование TSK-103."""
        if not rows:
            return 0
        await self._session.execute(
            _UPSERT,
            [
                {
                    "nm_id": row.nm_id,
                    "vendor_code": row.vendor_code,
                    "brand": row.brand,
                    "subject_name": row.subject_name,
                    "size": row.size,
                    "title": row.title,
                    "account_key": row.account_key,
                    "is_monitored": row.is_monitored,
                }
                for row in rows
            ],
        )
        return len(rows)

    async def count(self) -> int:
        result = await self._session.execute(text("select count(*) from wb.products"))
        return int(result.scalar_one())

    async def monitored_nm_ids(self, account_key: str | None = None) -> list[int]:
        query = "select nm_id from wb.products where is_monitored"
        params: dict[str, str] = {}
        if account_key is not None:
            query += " and account_key = :account_key"
            params["account_key"] = account_key
        result = await self._session.execute(text(query + " order by nm_id"), params)
        return [int(row[0]) for row in result.all()]

    async def catalog(
        self, *, q: str | None = None, account_key: str | None = None, limit: int = 100
    ) -> tuple[list[dict[str, object]], int]:
        conditions: list[str] = []
        params: dict[str, object] = {"limit": limit}
        if q:
            conditions.append(
                "(p.title ilike :q or p.vendor_code ilike :q or p.nm_id::text ilike :q)"
            )
            params["q"] = f"%{q}%"
        if account_key:
            conditions.append("p.account_key = :account_key")
            params["account_key"] = account_key
        where = " where " + " and ".join(conditions) if conditions else ""
        total = await self._session.execute(
            text("select count(*) from wb.products p" + where), params
        )
        rows = await self._session.execute(
            text(
                """
                select p.nm_id, p.vendor_code, p.title, p.account_key, p.is_monitored
                  from wb.products p
            """
                + where
                + " order by p.title nulls last, p.vendor_code, p.nm_id limit :limit"
            ),
            params,
        )
        return ([dict(row._mapping) for row in rows.all()], int(total.scalar_one()))

    async def add_to_monitoring(self, nm_ids: Sequence[int]) -> tuple[int, int]:
        ids = sorted(set(nm_ids))
        if not ids:
            return 0, 0
        rows = await self._session.execute(
            text("select nm_id, is_monitored from wb.products where nm_id = any(:ids) for update"),
            {"ids": ids},
        )
        existing = rows.all()
        if len(existing) != len(ids):
            missing = sorted(set(ids) - {int(row.nm_id) for row in existing})
            raise ProductNotFoundError(missing)
        already = sum(1 for row in existing if row.is_monitored)
        await self._session.execute(
            text("""
                update wb.products
                   set is_monitored = true, monitoring_added_manually = true, updated_at = now()
                 where nm_id = any(:ids) and not is_monitored
            """),
            {"ids": ids},
        )
        return len(ids) - already, already


class ProductNotFoundError(ValueError):
    def __init__(self, nm_ids: Sequence[int]) -> None:
        super().__init__(f"Неизвестные товары: {', '.join(map(str, nm_ids))}")
