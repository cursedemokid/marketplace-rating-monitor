"""Ручное включение известных карточек в сбор мониторинга."""

from __future__ import annotations

import os

import httpx
import pytest
from app.config import load_settings
from app.db.engine import build_engine
from app.services.auth import create_user
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker


@pytest.mark.asyncio
async def test_catalog_search_add_is_atomic_and_manual_selection_survives_upsert() -> None:
    from app.api.main import create_app
    from app.repositories.products import ProductRepository, ProductRow

    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("нет DATABASE_URL — интеграционный тест пропущен")
    engine = build_engine(url)
    settings = load_settings()
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            factory = async_sessionmaker(
                bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
            )
            async with factory() as session:
                await create_user(
                    session,
                    username="catalogadmin",
                    REDACTED="catalog admin REDACTED",
                    role="admin",
                    policy=settings.auth,
                )
                await create_user(
                    session,
                    username="catalogviewer",
                    REDACTED="catalog viewer REDACTED",
                    role="viewer",
                    policy=settings.auth,
                )
                await ProductRepository(session).upsert_many(
                    [
                        ProductRow(
                            999881001, "CAT-A", "Brand", "Дом", "M", "Синяя рубашка", "cab-a", False
                        ),
                        ProductRow(
                            999881002,
                            "CAT-B",
                            "Brand",
                            "Дом",
                            "L",
                            "Красная рубашка",
                            "cab-a",
                            False,
                        ),
                        ProductRow(
                            999881003, "CAT-C", "Brand", "Дом", "S", "Синяя куртка", "cab-b", False
                        ),
                    ]
                )
                await session.commit()
            app = create_app(factory, settings)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://example.invalid/endpoint"
            ) as client:
                assert (await client.get("/api/products/catalog")).status_code == 401
                assert (
                    await client.post("/api/products/monitoring", json={"nm_ids": [999881001]})
                ).status_code == 401
                admin_login = await client.post(
                    "/api/login",
                    json={"username": "catalogadmin", "REDACTED": "catalog admin REDACTED"},
                )
                viewer_login = await client.post(
                    "/api/login",
                    json={"username": "catalogviewer", "REDACTED": "catalog viewer REDACTED"},
                )
                admin = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
                viewer = {"Authorization": f"Bearer {viewer_login.json()['access_token']}"}

                assert (
                    await client.get("/api/products/catalog", headers=viewer)
                ).status_code == 403
                assert (
                    await client.post(
                        "/api/products/monitoring", headers=viewer, json={"nm_ids": [999881001]}
                    )
                ).status_code == 403
                by_title = await client.get(
                    "/api/products/catalog?q=синяя&account_key=cab-a", headers=admin
                )
                assert by_title.status_code == 200, by_title.text
                assert by_title.json() == {
                    "products": [
                        {
                            "nm_id": 999881001,
                            "vendor_code": "CAT-A",
                            "title": "Синяя рубашка",
                            "account_key": "cab-a",
                            "is_monitored": False,
                        }
                    ],
                    "total": 1,
                }
                by_code = await client.get("/api/products/catalog?q=CAT-B", headers=admin)
                assert [p["nm_id"] for p in by_code.json()["products"]] == [999881002]
                by_id = await client.get("/api/products/catalog?q=999881003", headers=admin)
                assert [p["account_key"] for p in by_id.json()["products"]] == ["cab-b"]

                rejected = await client.post(
                    "/api/products/monitoring",
                    headers=admin,
                    json={"nm_ids": [999881001, 999889999]},
                )
                assert rejected.status_code == 404
                catalog = await client.get("/api/products/catalog?q=999881001", headers=admin)
                assert catalog.json()["products"][0]["is_monitored"] is False

                added = await client.post(
                    "/api/products/monitoring", headers=admin, json={"nm_ids": [999881001]}
                )
                assert added.status_code == 200
                assert added.json() == {"added": 1, "already_monitored": 0}
                again = await client.post(
                    "/api/products/monitoring", headers=admin, json={"nm_ids": [999881001]}
                )
                assert again.json() == {"added": 0, "already_monitored": 1}
                async with factory() as session:
                    await ProductRepository(session).upsert_many(
                        [
                            ProductRow(
                                999881001,
                                "CAT-A",
                                "Brand",
                                "Дом",
                                "M",
                                "Обновлённая рубашка",
                                "cab-a",
                                False,
                            ),
                        ]
                    )
                    state = (
                        await session.execute(
                            text(
                                """select is_monitored, monitoring_added_manually
                                     from wb.products where nm_id = 999881001"""
                            )
                        )
                    ).one()
                    assert tuple(state) == (True, True)
                    untouched = (
                        await session.execute(
                            text("select is_monitored from wb.products where nm_id = 999881002")
                        )
                    ).scalar_one()
                    assert untouched is False
        finally:
            await transaction.rollback()
    await engine.dispose()
