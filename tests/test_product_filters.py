"""Одна выборка товаров обслуживает экран и обе выгрузки."""

from __future__ import annotations

from app.repositories.product_filters import ProductFilters, filter_options, select_product_ids
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def test_combined_product_filters_and_options(session: AsyncSession) -> None:
    products = [
        (999_008_001, "FILTER-ONE", "Alpha", "Подгузники", "M", "Ночные", True),
        (999_008_002, "FILTER-TWO", "Alpha", "Подгузники", "L", "Дневные", True),
        (999_008_003, "FILTER-HIDDEN", "Beta", "Салфетки", "M", "Ночные", False),
    ]
    for nm_id, code, brand, subject, size, title, monitored in products:
        await session.execute(
            text("""
                insert into wb.products
                    (nm_id, vendor_code, brand, subject_name, size, title,
                     is_monitored, updated_at)
                values (:nm_id, :code, :brand, :subject, :size, :title,
                        :monitored, now())
            """),
            {
                "nm_id": nm_id,
                "code": code,
                "brand": brand,
                "subject": subject,
                "size": size,
                "title": title,
                "monitored": monitored,
            },
        )
    for code, pack, line, kind, capacity, category, brand2, kit, qty in [
        ("FILTER-ONE", "1", "Premium", "А", "10", "Детское", "Бренд 2", "Да", 2),
        ("FILTER-TWO", "2", "Basic", "Б", "20", "Детское", "Бренд 3", "Нет", 1),
    ]:
        await session.execute(
            text("""
                insert into wb.nomenclature
                    (vendor_code, pack, line, type, capacity, category, brand2,
                     kit, kit_qty, imported_at)
                values (:code, :pack, :line, :kind, :capacity, :category,
                        :brand2, :kit, :qty, now())
            """),
            {
                "code": code,
                "pack": pack,
                "line": line,
                "kind": kind,
                "capacity": capacity,
                "category": category,
                "brand2": brand2,
                "kit": kit,
                "qty": qty,
            },
        )

    one = 999_008_001
    dimensions = {
        "vendor_code": "FILTER-ONE",
        "wb_code": one,
        "brand": "Alpha",
        "subject_name": "Подгузники",
        "size": "M",
        "nomenclature": "Ночные",
        "pack": "1",
        "line": "Premium",
        "type": "А",
        "capacity": "10",
        "category": "Детское",
        "brand2": "Бренд 2",
        "kit": "Да",
        "kit_qty": 2,
    }
    assert await select_product_ids(session, ProductFilters(**dimensions)) == [one]
    mismatches = {
        "wb_code": 999_008_002,
        "brand": "Beta",
        "subject_name": "Салфетки",
        "size": "L",
        "nomenclature": "Дневные",
        "pack": "2",
        "line": "Basic",
        "type": "Б",
        "capacity": "20",
        "category": "Другое",
        "brand2": "Бренд 3",
        "kit": "Нет",
        "kit_qty": 1,
    }
    for field, value in mismatches.items():
        ids = await select_product_ids(
            session, ProductFilters(vendor_code="FILTER-ONE", **{field: value})
        )
        assert ids == [], field
    assert (
        await select_product_ids(session, ProductFilters(brand="Alpha", size="L", pack="1")) == []
    )
    assert await select_product_ids(session, ProductFilters(q="filter-one")) == [one]
    assert await select_product_ids(session, ProductFilters(q="ночные")) == [one]
    assert await select_product_ids(session, ProductFilters(wb_code=999_008_003)) == []
    assert await select_product_ids(session, ProductFilters()) is None

    await session.execute(
        text("update wb.products set account_key = 'DemoSeller' where nm_id = :nm_id"),
        {"nm_id": 999_008_002},
    )
    await session.execute(
        text("update wb.nomenclature set account_key = 'DemoSeller' where vendor_code = 'FILTER-TWO'")
    )
    assert await select_product_ids(session, ProductFilters(account_key="default")) == [one]
    assert await select_product_ids(session, ProductFilters(account_key="DemoSeller")) == [999_008_002]

    options = await filter_options(session)
    assert options["account_key"] == ["default", "DemoSeller"]
    assert "Ночные" in options["nomenclature"]
    assert options["kit_qty"] == [1, 2]
    assert "Beta" not in options["brand"]


async def test_nomenclature_join_uses_account_and_vendor_code(session: AsyncSession) -> None:
    for nm_id, account_key in [(999_008_101, "default"), (999_008_102, "DemoSeller")]:
        await session.execute(
            text("""
                insert into wb.products
                    (nm_id, account_key, vendor_code, is_monitored, updated_at)
                values (:nm_id, :account_key, 'SHARED-CODE', true, now())
            """),
            {"nm_id": nm_id, "account_key": account_key},
        )
    await session.execute(
        text("""
            insert into wb.nomenclature
                (account_key, vendor_code, pack, imported_at)
            values ('default', 'SHARED-CODE', '1', now()),
                   ('DemoSeller', 'SHARED-CODE', '2', now())
        """)
    )
    assert await select_product_ids(session, ProductFilters(pack="1")) == [999_008_101]
    assert await select_product_ids(session, ProductFilters(pack="2")) == [999_008_102]
    options = await filter_options(session)
    assert "1" in options["pack"] and "2" in options["pack"]
