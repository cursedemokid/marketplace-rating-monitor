"""Репозиторий клиентской номенклатуры, ключ — кабинет и артикул продавца."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class NomenclatureInput:
    account_key: str
    vendor_code: str
    pack: str | None = None
    line: str | None = None
    type: str | None = None
    capacity: str | None = None
    category: str | None = None
    brand2: str | None = None
    kit: str | None = None
    kit_qty: int | None = None


@dataclass(frozen=True, slots=True)
class NomenclatureRecord(NomenclatureInput):
    pass


@dataclass(frozen=True, slots=True)
class ProductIdentity:
    account_key: str
    vendor_code: str
    nm_id: int


_FIELDS = ("pack", "line", "type", "capacity", "category", "brand2", "kit", "kit_qty")
_UPSERT = text("""
    insert into wb.nomenclature (
        account_key, vendor_code, pack, line, type, capacity, category, brand2, kit, kit_qty,
        imported_at
    )
    values (
        :account_key, :vendor_code, :pack, :line, :type, :capacity, :category, :brand2, :kit,
        :kit_qty, now()
    )
    on conflict (account_key, vendor_code) do update set
        pack = excluded.pack,
        line = excluded.line,
        type = excluded.type,
        capacity = excluded.capacity,
        category = excluded.category,
        brand2 = excluded.brand2,
        kit = excluded.kit,
        kit_qty = excluded.kit_qty,
        imported_at = now()
""")


class NomenclatureRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def products(self) -> list[ProductIdentity]:
        result = await self._session.execute(
            text("select account_key, vendor_code, nm_id from wb.products")
        )
        return [
            ProductIdentity(str(row.account_key), str(row.vendor_code), int(row.nm_id))
            for row in result.all()
        ]

    async def upsert_many(self, rows: Sequence[NomenclatureInput]) -> int:
        if not rows:
            return 0
        await self._session.execute(
            _UPSERT,
            [
                {
                    "account_key": row.account_key,
                    "vendor_code": row.vendor_code,
                    **{field: getattr(row, field) for field in _FIELDS},
                }
                for row in rows
            ],
        )
        return len(rows)

    async def get(self, account_key: str, vendor_code: str) -> NomenclatureRecord | None:
        result = await self._session.execute(
            text("""
                select account_key, vendor_code, pack, line, type, capacity, category, brand2,
                       kit, kit_qty
                  from wb.nomenclature
                 where account_key = :account_key and vendor_code = :vendor_code
            """),
            {"account_key": account_key, "vendor_code": vendor_code},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return NomenclatureRecord(**dict(row))

    async def update_fields(
        self, account_key: str, vendor_code: str, fields: dict[str, str | int | None]
    ) -> NomenclatureRecord | None:
        if not fields:
            return await self.get(account_key, vendor_code)
        assignments = ", ".join(f"{field} = :{field}" for field in fields)
        result = await self._session.execute(
            text(f"""
                update wb.nomenclature
                   set {assignments}, imported_at = now()
                 where account_key = :account_key and vendor_code = :vendor_code
             returning account_key, vendor_code, pack, line, type, capacity, category, brand2,
                       kit, kit_qty
            """),
            {
                "account_key": account_key,
                "vendor_code": vendor_code,
                **fields,
            },
        )
        found = result.mappings().one_or_none()
        return NomenclatureRecord(**dict(found)) if found is not None else None
