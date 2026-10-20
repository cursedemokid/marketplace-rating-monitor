"""Общий отбор отслеживаемых артикулов для экрана и Excel."""

from __future__ import annotations

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class ProductFilters(BaseModel):
    # q сохраняет прежний быстрый поиск; остальные поля соответствуют FR-8.1.
    q: str | None = Field(default=None, max_length=200)
    account_key: str | None = None
    vendor_code: str | None = None
    wb_code: int | None = None
    pack: str | None = None
    line: str | None = None
    type: str | None = None
    size: str | None = None
    capacity: str | None = None
    category: str | None = None
    subject_name: str | None = None
    brand: str | None = None
    brand2: str | None = None
    nomenclature: str | None = None
    kit: str | None = None
    kit_qty: int | None = None


_COLUMNS = {
    "account_key": "p.account_key",
    "vendor_code": "p.vendor_code",
    "wb_code": "p.nm_id",
    "pack": "n.pack",
    "line": "n.line",
    "type": "n.type",
    "size": "p.size",
    "capacity": "n.capacity",
    "category": "n.category",
    "subject_name": "p.subject_name",
    "brand": "p.brand",
    "brand2": "n.brand2",
    "nomenclature": "p.title",
    "kit": "n.kit",
    "kit_qty": "n.kit_qty",
}


async def select_product_ids(
    session: AsyncSession, filters: ProductFilters, *, nm_ids: list[int] | None = None
) -> list[int] | None:
    """`None` означает все отслеживаемые товары, `[]` — ни одного совпадения."""
    clauses: list[str] = []
    params: dict[str, object] = {}
    for field, column in _COLUMNS.items():
        value = getattr(filters, field)
        if isinstance(value, str):
            value = value.strip()
        if value is None or value == "":
            continue
        clauses.append(f"{column} = :{field}")
        params[field] = value

    query = (filters.q or "").strip()
    if query:
        clauses.append("""(
            position(lower(:q) in lower(p.vendor_code)) > 0
            or position(lower(:q) in lower(coalesce(p.title, ''))) > 0
            or p.nm_id::text = :q
        )""")
        params["q"] = query
    if nm_ids is not None:
        clauses.append("p.nm_id = any(:nm_ids)")
        params["nm_ids"] = nm_ids
    if not clauses:
        return None

    selected = " and ".join(clauses)
    result = await session.execute(
        text(f"""
            select p.nm_id
              from wb.products p
              left join wb.nomenclature n
                on n.account_key = p.account_key and n.vendor_code = p.vendor_code
             where p.is_monitored and {selected}
             order by p.nm_id
        """),
        params,
    )
    return [int(row.nm_id) for row in result.all()]


async def filter_options(session: AsyncSession) -> dict[str, list[str | int]]:
    """Значения селекторов только из отслеживаемых карточек и их номенклатуры."""
    options: dict[str, list[str | int]] = {}
    for field, column in _COLUMNS.items():
        if field in {"vendor_code", "wb_code"}:
            continue
        result = await session.execute(
            text(f"""
                select distinct {column} as value
                  from wb.products p
                  left join wb.nomenclature n
                    on n.account_key = p.account_key and n.vendor_code = p.vendor_code
                 where p.is_monitored and {column} is not null
                 order by value
            """)
        )
        options[field] = [
            row.value for row in result.all() if not isinstance(row.value, str) or row.value.strip()
        ]
    return options
