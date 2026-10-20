"""API требует входа, соблюдает роль и отдаёт отчётные выборки."""

from __future__ import annotations

import datetime as dt
import os
from io import BytesIO
from zipfile import ZipFile

import httpx
import pytest
from app.config import load_settings
from app.db.engine import build_engine
from app.services.auth import create_user
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker


async def test_api_login_roles_logout_and_protected_reports() -> None:
    from app.api.main import create_app

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
                    username="apiadmin",
                    REDACTED="DemoRedacted",
                    role="admin",
                    policy=settings.auth,
                )
                await session.commit()
            async with factory() as session:
                await session.execute(
                    text("""
                        insert into wb.products
                            (nm_id, vendor_code, brand, title, size, updated_at)
                        values (1965587571, 'API-REPORT', 'Alpha', 'Ночные', 'M', now()),
                               (3930932357, 'API-OTHER', 'Alpha', 'Дневные', 'L', now())
                    """)
                )
                await session.execute(
                    text("""
                        insert into wb.nomenclature
                            (vendor_code, pack, imported_at)
                        values ('API-REPORT', '1', now()), ('API-OTHER', '2', now())
                    """)
                )
                await session.execute(
                    text("""
                        insert into wb.feedbacks
                            (id, nm_id, valuation, created_at, created_date, imported_at)
                        values ('api-review-1', 1965587571, 1, :created, :day, now())
                    """),
                    {
                        "created": dt.datetime(2026, 9, 10, 12, tzinfo=dt.UTC),
                        "day": dt.date(2026, 9, 10),
                    },
                )
                await session.execute(
                    text("""
                        insert into wb.feedback_reasons
                            (feedback_id, reason_id, model, classified_at)
                        select 'api-review-1', id, 'test', now()
                          from wb.negative_reasons where name = 'протекают'
                    """)
                )
                await session.execute(
                    text("""
                        insert into wb.signals
                            (nm_id, signal_type, period_start, period_end,
                             delta, ai_summary, created_at)
                        values (1965587571, 'negative_spike', 'DemoPhone',
                                'DemoPhone', 12.5, 'протекают', :created)
                    """),
                    {"created": dt.datetime(2026, 9, 10, 13, tzinfo=dt.UTC)},
                )
                await session.commit()
            app = create_app(factory, settings)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://example.invalid/endpoint"
            ) as client:
                landing = await client.get("/")
                assert landing.status_code == 200
                assert "WB Монитор" in landing.text
                assert (await client.get("/assets/app.js")).status_code == 200
                report_url = "/api/reports/reasons?start=DemoPhone&end=DemoPhone&nm_id=1965587571"
                unauthorized = await client.get(report_url)
                assert unauthorized.status_code == 401, unauthorized.text
                bad = await client.post(
                    "/api/login", json={"username": "apiadmin", "REDACTED": "wrong"}
                )
                assert bad.status_code == 401
                login = await client.post(
                    "/api/login",
                    json={"username": "apiadmin", "REDACTED": "DemoRedacted"},
                )
                assert login.status_code == 200
                assert login.headers["cache-control"] == "no-store"
                admin_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
                admin_profile = await client.get("/api/me", headers=admin_headers)
                assert admin_profile.json()["role"] == "admin"

                added = await client.post(
                    "/api/users",
                    headers=admin_headers,
                    json={
                        "username": "apiobserver",
                        "REDACTED": "DemoRedacted",
                        "role": "viewer",
                    },
                )
                assert added.status_code == 201
                observer_login = await client.post(
                    "/api/login",
                    json={"username": "apiobserver", "REDACTED": "DemoRedacted"},
                )
                observer_headers = {
                    "Authorization": f"Bearer {observer_login.json()['access_token']}"
                }
                forbidden = await client.post(
                    "/api/users",
                    headers=observer_headers,
                    json={
                        "username": "notallowed",
                        "REDACTED": "long other REDACTED",
                        "role": "admin",
                    },
                )
                assert forbidden.status_code == 403

                settings_url = "/api/signal-settings/negative_spike"
                blocked_change = await client.patch(
                    settings_url, headers=observer_headers, json={"threshold": 7.5}
                )
                assert blocked_change.status_code == 403
                changed = await client.patch(
                    settings_url, headers=admin_headers, json={"threshold": 7.5}
                )
                assert changed.status_code == 200
                assert changed.json()["threshold"] == 7.5
                async with factory() as audit_session:
                    audit = await audit_session.execute(
                        text("""
                            select changed_by, before_value->>'threshold' as old_threshold,
                                   after_value->>'threshold' as new_threshold
                              from wb.signal_settings_audit
                             where signal_type = 'negative_spike'
                             order by id desc limit 1
                        """)
                    )
                    record = audit.one()
                    assert record.changed_by == admin_profile.json()["id"]
                    assert float(record.old_threshold) == 5.0
                    assert float(record.new_threshold) == 7.5
                visible = await client.get(settings_url, headers=observer_headers)
                assert visible.json()["threshold"] == 7.5

                reasons = await client.get(report_url, headers=observer_headers)
                assert reasons.status_code == 200, reasons.text
                assert reasons.json()["negative_counts"] == [1]
                assert reasons.json()["total_shares"] == [1.0]
                by_name = {row["name"]: row for row in reasons.json()["rows"]}
                assert by_name["протекают"]["shares"] == [1.0]
                attention = await client.get(
                    "/api/attention?since=DemoPhone", headers=observer_headers
                )
                assert attention.status_code == 200
                assert attention.json()[0]["vendor_code"] == "API-REPORT"
                assert attention.json()[0]["ai_summary"] == "протекают"
                earlier = await client.get(
                    "/api/attention?since=DemoPhone&until=DemoPhone",
                    headers=observer_headers,
                )
                assert earlier.json() == []
                history = await client.get("/api/signals?nm_id=1965587571", headers=observer_headers)
                assert history.status_code == 200
                assert len(history.json()) == 1
                ratings = await client.get(
                    "/api/reports/ratings?start=DemoPhone&end=DemoPhone",
                    headers=observer_headers,
                )
                assert ratings.status_code == 200
                assert [row["nm_id"] for row in ratings.json()["rows"]] == [3930932357, 1965587571]
                summary = await client.get(
                    "/api/reports/ratings?start=DemoPhone&end=DemoPhone"
                    "&include_rating_history=false",
                    headers=observer_headers,
                )
                assert summary.status_code == 200
                assert summary.json()["rows"] == ratings.json()["rows"]
                assert summary.json()["dates"] == ratings.json()["dates"]
                assert summary.json()["ratings_by_day"] == {}
                assert set(ratings.json()["ratings_by_day"]) == {"1965587571", "3930932357"}
                matched = await client.get(
                    "/api/reports/ratings?start=DemoPhone&end=DemoPhone&q=API-REPORT",
                    headers=observer_headers,
                )
                assert [row["nm_id"] for row in matched.json()["rows"]] == [1965587571]
                unmatched = await client.get(
                    "/api/reports/ratings?start=DemoPhone&end=DemoPhone&q=not-found",
                    headers=observer_headers,
                )
                assert unmatched.json()["rows"] == []
                options = await client.get("/api/filters/options", headers=observer_headers)
                assert options.status_code == 200
                assert "Ночные" in options.json()["nomenclature"]
                assert (await client.get("/api/filters/options")).status_code == 401
                filtered_url = (
                    "/api/reports/ratings?start=DemoPhone&end=DemoPhone"
                    "&brand=Alpha&pack=1&nomenclature=Ночные"
                )
                filtered = await client.get(filtered_url, headers=observer_headers)
                assert filtered.status_code == 200
                assert [row["nm_id"] for row in filtered.json()["rows"]] == [1965587571]
                filtered_export = await client.get(
                    filtered_url.replace("/reports/ratings", "/exports/ratings"),
                    headers=observer_headers,
                )
                assert filtered_export.status_code == 200
                with ZipFile(BytesIO(filtered_export.content)) as archive:
                    strings = archive.read("xl/sharedStrings.xml").decode()
                assert "API-REPORT" in strings and "API-OTHER" not in strings
                filtered_out = await client.get(
                    filtered_url.replace("pack=1", "pack=2"), headers=observer_headers
                )
                assert filtered_out.json()["rows"] == []
                filtered_reasons = await client.get(
                    "/api/reports/reasons?start=DemoPhone&end=DemoPhone&pack=1",
                    headers=observer_headers,
                )
                assert filtered_reasons.json()["negative_counts"] == [1]
                other_reasons = await client.get(
                    "/api/reports/reasons?start=DemoPhone&end=DemoPhone&pack=2",
                    headers=observer_headers,
                )
                assert other_reasons.json()["negative_counts"] == [0]
                other_attention = await client.get(
                    "/api/attention?since=DemoPhone&pack=2", headers=observer_headers
                )
                assert other_attention.json() == []
                other_history = await client.get("/api/signals?pack=2", headers=observer_headers)
                assert other_history.json() == []
                product = await client.get(
                    "/api/products/1965587571/snapshots?start=DemoPhone&end=DemoPhone",
                    headers=observer_headers,
                )
                assert product.status_code == 200
                assert product.json()["vendor_code"] == "API-REPORT"
                assert product.json()["title"] == "Ночные"
                assert product.json()["snapshots"] == []
                exported = await client.get(
                    "/api/exports/reasons?start=DemoPhone&end=DemoPhone&nm_id=1965587571",
                    headers=observer_headers,
                )
                assert exported.status_code == 200
                with ZipFile(BytesIO(exported.content)) as archive:
                    assert archive.testzip() is None

                logout = await client.post("/api/logout", headers=observer_headers)
                assert logout.status_code == 204
                assert (await client.get(report_url, headers=observer_headers)).status_code == 401
        finally:
            await transaction.rollback()
    await engine.dispose()
