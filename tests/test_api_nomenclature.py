"""Admin preview/import/edit rights and one filtered report/export selection."""

from __future__ import annotations

import os
from io import BytesIO
from zipfile import ZipFile

import httpx
import pytest
import xlsxwriter
from app.api.main import create_app
from app.config import load_settings
from app.db.engine import build_engine
from app.services.auth import create_user
from sqlalchemy import text


def workbook() -> bytes:
    output = BytesIO()
    book = xlsxwriter.Workbook(output, {"in_memory": True})
    sheet = book.add_worksheet()
    sheet.write_row(
        5,
        0,
        [
            "Кабинет",
            "Код WB",
            "Артикул продавца",
            "Пачка",
            "Линейка",
            "Тип",
            "Вложимость",
            "Категория",
            "Бренд-2",
            "Комплект",
            "Количество в комплекте",
        ],
    )
    sheet.write_row(
        6, 0, ["Элалис", 999_081_001, "API-SAME", "Пачка Э", "", "ТООО «Пример»", "Категория", "", "", ""]
    )
    book.close()
    return output.getvalue()


async def test_admin_import_and_edit_are_protected_and_drive_excel_filters() -> None:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("нет DATABASE_URL")
    settings = load_settings()
    engine = build_engine(url)
    try:
        async with engine.connect() as connection:
            outer = await connection.begin()
            try:
                from sqlalchemy.ext.asyncio import async_sessionmaker

                scoped = async_sessionmaker(
                    bind=connection,
                    expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                async with scoped() as session:
                    await create_user(
                        session,
                        username="nomenadmin",
                        REDACTED="DemoRedacted",
                        role="admin",
                        policy=settings.auth,
                    )
                    await create_user(
                        session,
                        username="nomenviewer",
                        REDACTED="DemoRedacted",
                        role="viewer",
                        policy=settings.auth,
                    )
                    await session.execute(
                        text("""
                        insert into wb.products
                            (nm_id, account_key, vendor_code, is_monitored, updated_at)
                        values (2910597427, 'DemoSeller', 'API-SAME', true, now()),
                               (2741649964, 'default', 'API-SAME', true, now())
                    """)
                    )
                    await session.commit()
                app = create_app(scoped, settings)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="https://example.invalid/endpoint"
                ) as client:
                    admin = (
                        await client.post(
                            "/api/login",
                            json={
                                "username": "nomenadmin",
                                "REDACTED": "DemoRedacted",
                            },
                        )
                    ).json()["access_token"]
                    viewer = (
                        await client.post(
                            "/api/login",
                            json={
                                "username": "nomenviewer",
                                "REDACTED": "DemoRedacted",
                            },
                        )
                    ).json()["access_token"]
                    ah = {"Authorization": f"Bearer {admin}"}
                    vh = {"Authorization": f"Bearer {viewer}"}
                    raw = workbook()
                    assert (
                        await client.post("/api/nomenclature/preview", headers=vh, content=raw)
                    ).status_code == 403
                    preview = await client.post(
                        "/api/nomenclature/preview", headers=ah, content=raw
                    )
                    assert preview.status_code == 200, preview.text
                    assert preview.json()["valid_count"] == 1
                    assert preview.json()["can_import"] is True
                    assert preview.json()["rows"][0]["brand2"] is None
                    assert (
                        await client.post("/api/nomenclature/import", headers=vh, content=raw)
                    ).status_code == 403
                    imported = await client.post(
                        "/api/nomenclature/import", headers=ah, content=raw
                    )
                    assert imported.status_code == 200, imported.text
                    assert imported.json()["imported"] == 1
                    path = "/api/nomenclature/DemoSeller/API-SAME"
                    assert (
                        await client.patch(path, headers=vh, json={"pack": "Другая"})
                    ).status_code == 403
                    changed = await client.patch(path, headers=ah, json={"line": "Линейка Э"})
                    assert changed.status_code == 200
                    assert changed.json()["brand2"] is None
                    assert changed.json()["pack"] == "Пачка Э"
                    report = await client.get(
                        "/api/reports/ratings?start=DemoPhone&end=DemoPhone&pack=Пачка%20Э",
                        headers=vh,
                    )
                    assert [row["nm_id"] for row in report.json()["rows"]] == [999_081_001]
                    export = await client.get(
                        "/api/exports/ratings?start=DemoPhone&end=DemoPhone&pack=Пачка%20Э",
                        headers=vh,
                    )
                    assert export.status_code == 200
                    with ZipFile(BytesIO(export.content)) as archive:
                        strings = archive.read("xl/sharedStrings.xml").decode()
                    assert "API-SAME" in strings
                    assert (await client.get("/api/ai/costs", headers=vh)).status_code == 403
                    assert (
                        await client.get("/api/moderation/candidates", headers=vh)
                    ).status_code == 403
                    ai_response = await client.get("/api/ai/costs", headers=ah)
                    assert ai_response.status_code == 200
                    ai_status = ai_response.json()
                    assert "remaining_usd" in ai_status
                    assert "pending_text" in ai_status
                    assert "pending_blank" in ai_status
                    assert "claimed_without_labels" in ai_status
                    assert "unlabeled_claims" in ai_status
                    assert "classify_runs" in ai_status
            finally:
                await outer.rollback()
    finally:
        await engine.dispose()
