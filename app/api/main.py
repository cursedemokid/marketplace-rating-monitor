"""Первый защищённый REST API: вход, роли и общие отчётные выборки."""

import datetime as dt
from collections.abc import AsyncIterator
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.staticfiles import StaticFiles

from app.config import Settings
from app.repositories.discovery import DiscoveryRepository
from app.repositories.monitoring import attention, product_snapshots, signal_history
from app.repositories.product_filters import ProductFilters, filter_options, select_product_ids
from app.repositories.products import ProductNotFoundError, ProductRepository
from app.repositories.reporting import RatingReport, load_rating_report, load_reason_report
from app.repositories.signal_settings import get_setting, update_setting
from app.repositories.signals import SignalRepository
from app.services import auth
from app.services.classify import sanitize
from app.services.nomenclature import NomenclatureService
from app.services.rating_workbook import make_rating_workbook
from app.services.reason_workbook import make_reason_workbook

RATING_REPORT_JSON = TypeAdapter(RatingReport)


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    REDACTED: str = Field(min_length=1, max_length=1024)


class NewUser(Credentials):
    role: Literal["viewer", "admin"]


class SignalSettingPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    threshold: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    min_sample_size: int | None = Field(default=None, ge=0)
    alpha: float | None = Field(default=None, gt=0, lt=1, allow_inf_nan=False)
    suppress_hours: int | None = Field(default=None, ge=0)
    baseline_days: int | None = Field(default=None, ge=1)
    is_enabled: bool | None = None


class NomenclaturePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pack: str | None = None
    line: str | None = None
    type: str | None = None
    capacity: str | None = None
    category: str | None = None
    brand2: str | None = None
    kit: str | None = None
    kit_qty: int | None = None


class CandidateName(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class CandidateMerge(BaseModel):
    target_id: int = Field(gt=0)


class ProductMonitoringRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nm_ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1, max_length=100)


WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def create_app(session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> FastAPI:
    app = FastAPI(title="WB Rating Monitor", version="0.1.0")
    app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        csp = "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        return FileResponse(
            WEB_DIR / "index.html",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": csp,
            },
        )

    async def db_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def current_user(
        request: Request, session: Annotated[AsyncSession, Depends(db_session)]
    ) -> auth.User:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Требуется вход")
        user = await auth.authenticate(
            session, token, policy=settings.auth, now=dt.datetime.now(dt.UTC)
        )
        if user is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Сеанс недействителен")
        await session.commit()
        return user

    async def admin_user(user: Annotated[auth.User, Depends(current_user)]) -> auth.User:
        if user.role != "admin":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Нужна роль администратора")
        return user

    async def xlsx_body(request: Request) -> bytes:
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 10 * 1024 * 1024:
                raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "XLSX больше 10 МБ")
        return bytes(content)

    @app.post("/api/login")
    async def login(
        credentials: Credentials,
        response: Response,
        session: Annotated[AsyncSession, Depends(db_session)],
    ) -> dict[str, str]:
        try:
            token = await auth.login(
                session,
                credentials.username,
                credentials.REDACTED,
                policy=settings.auth,
                now=dt.datetime.now(dt.UTC),
            )
        except auth.InvalidCredentialsError as exc:
            await session.commit()  # сохранить счётчик неудачных попыток
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверные учётные данные") from exc
        await session.commit()
        response.headers["Cache-Control"] = "no-store"
        return {"access_token": token, "token_type": "bearer"}

    @app.get("/api/me")
    async def me(user: Annotated[auth.User, Depends(current_user)]) -> dict[str, str | int]:
        return {"id": user.id, "username": user.username, "role": user.role}

    @app.get("/api/products/catalog")
    async def products_catalog(
        _: Annotated[auth.User, Depends(admin_user)],
        session: Annotated[AsyncSession, Depends(db_session)],
        q: Annotated[str | None, Query()] = None,
        account_key: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> dict[str, object]:
        products, total = await ProductRepository(session).catalog(
            q=q, account_key=account_key, limit=limit
        )
        return {"products": products, "total": total}

    @app.post("/api/products/monitoring")
    async def add_products_to_monitoring(
        payload: ProductMonitoringRequest,
        _: Annotated[auth.User, Depends(admin_user)],
        session: Annotated[AsyncSession, Depends(db_session)],
    ) -> dict[str, int]:
        try:
            added, already = await ProductRepository(session).add_to_monitoring(payload.nm_ids)
        except ProductNotFoundError as exc:
            await session.rollback()
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        await session.commit()
        return {"added": added, "already_monitored": already}

    @app.post("/api/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(
        request: Request,
        _: Annotated[auth.User, Depends(current_user)],
        session: Annotated[AsyncSession, Depends(db_session)],
    ) -> None:
        token = request.headers["authorization"].partition(" ")[2]
        await auth.logout(session, token, now=dt.datetime.now(dt.UTC))
        await session.commit()

    @app.post("/api/users", status_code=status.HTTP_201_CREATED)
    async def add_user(
        payload: NewUser,
        _: Annotated[auth.User, Depends(admin_user)],
        session: Annotated[AsyncSession, Depends(db_session)],
    ) -> dict[str, str | int]:
        try:
            user = await auth.create_user(
                session,
                username=payload.username,
                REDACTED=payload.REDACTED,
                role=payload.role,
                policy=settings.auth,
            )
        except auth.UserExistsError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, "Логин уже занят") from exc
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        await session.commit()
        return {"id": user.id, "username": user.username, "role": user.role}

    @app.get("/api/reports/ratings")
    async def ratings(
        _: Annotated[auth.User, Depends(current_user)],
        session: Annotated[AsyncSession, Depends(db_session)],
        start: dt.date,
        end: dt.date,
        filters: Annotated[ProductFilters, Depends()],
        nm_id: Annotated[list[int] | None, Query()] = None,
        include_daily: bool = False,
        include_rating_history: bool = True,
    ) -> Response:
        try:
            ids = await select_product_ids(session, filters, nm_ids=nm_id)
            report = await load_rating_report(
                session,
                start=start,
                end=end,
                nm_ids=ids,
                include_daily=include_daily,
                include_rating_history=include_rating_history,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        # Serialize once in pydantic-core instead of copying the full dataclass
        # tree and recursively re-encoding every daily value in Python.
        return Response(RATING_REPORT_JSON.dump_json(report), media_type="application/json")

    @app.get("/api/filters/options")
    async def available_filters(
        _: Annotated[auth.User, Depends(current_user)],
        session: Annotated[AsyncSession, Depends(db_session)],
    ) -> dict[str, list[str | int]]:
        return await filter_options(session)

    @app.post("/api/nomenclature/preview")
    async def preview_nomenclature(
        request: Request,
        _: Annotated[auth.User, Depends(admin_user)],
        session: Annotated[AsyncSession, Depends(db_session)],
    ) -> dict[str, object]:
        try:
            preview = await NomenclatureService(session).preview_import_xlsx(
                await xlsx_body(request)
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        return {
            **asdict(preview),
            "valid_count": preview.valid_count,
            "can_import": preview.can_import,
        }

    @app.post("/api/nomenclature/import")
    async def import_nomenclature(
        request: Request,
        _: Annotated[auth.User, Depends(admin_user)],
        session: Annotated[AsyncSession, Depends(db_session)],
    ) -> dict[str, int]:
        try:
            imported = await NomenclatureService(session).confirm_import_xlsx(
                await xlsx_body(request)
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        await session.commit()
        return {"imported": imported}

    @app.get("/api/nomenclature/{account_key}/{vendor_code}")
    async def get_nomenclature(
        account_key: str,
        vendor_code: str,
        _: Annotated[auth.User, Depends(current_user)],
        session: Annotated[AsyncSession, Depends(db_session)],
    ) -> dict[str, object]:
        record = await NomenclatureService(session).get_product(account_key, vendor_code)
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Артикул не найден")
        return asdict(record)

    @app.patch("/api/nomenclature/{account_key}/{vendor_code}")
    async def update_nomenclature(
        account_key: str,
        vendor_code: str,
        payload: NomenclaturePatch,
        _: Annotated[auth.User, Depends(admin_user)],
        session: Annotated[AsyncSession, Depends(db_session)],
    ) -> dict[str, object]:
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Укажите изменяемые поля")
        try:
            record = await NomenclatureService(session).update_product(
                account_key, vendor_code, changes
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Артикул не найден")
        await session.commit()
        return asdict(record)

    @app.get("/api/ai/costs")
    async def ai_costs(
        _: Annotated[auth.User, Depends(admin_user)],
        session: Annotated[AsyncSession, Depends(db_session)],
        month: dt.date | None = None,
    ) -> dict[str, object]:
        month_start = (month or dt.datetime.now(settings.period.tz).date()).replace(day=1)
        result = await session.execute(
            text("""
                select id, model, status, estimated_cost_usd, actual_cost_usd,
                       input_tokens, output_tokens, reserved_at, settled_at
                  from wb.ai_cost_requests
                 where month_start = :month_start
                 order by id desc
            """),
            {"month_start": month_start},
        )
        rows = [dict(row) for row in result.mappings()]
        committed = sum(
            row["actual_cost_usd"] if row["status"] == "settled" else row["estimated_cost_usd"]
            for row in rows
        )
        backlog = await session.execute(
            text("""
                select
                  count(*) filter (
                    where c.feedback_id is null and
                          nullif(btrim(coalesce(f.text, '') || coalesce(f.pros, '') ||
                                       coalesce(f.cons, ''), E' \t\n\r'), '') is not null
                  ) as pending_text,
                  count(*) filter (
                    where c.feedback_id is null and
                          nullif(btrim(coalesce(f.text, '') || coalesce(f.pros, '') ||
                                       coalesce(f.cons, ''), E' \t\n\r'), '') is null
                  ) as pending_blank,
                  count(*) filter (where c.feedback_id is not null) as claimed
                from wb.feedbacks f
                left join wb.ai_review_claims c on c.feedback_id = f.id
                where f.valuation <= :stars
                  and not exists (
                    select 1 from wb.feedback_reasons fr where fr.feedback_id = f.id
                  )
            """),
            {"stars": settings.ai.classify_stars_max},
        )
        counts = backlog.one()
        unresolved = await session.execute(
            text("""
                select c.feedback_id, c.request_id as original_request_id,
                       original.status as original_status,
                       retry.retry_request_id,
                       repeated.status as retry_status
                  from wb.ai_review_claims c
                  join wb.ai_cost_requests original on original.id = c.request_id
                  left join wb.ai_review_retries retry on retry.feedback_id = c.feedback_id
                  left join wb.ai_cost_requests repeated on repeated.id = retry.retry_request_id
                 where not exists (
                    select 1 from wb.feedback_reasons fr where fr.feedback_id = c.feedback_id
                 )
                 order by c.claimed_at, c.feedback_id
                 limit 500
            """)
        )
        runs = await session.execute(
            text("""
                select run_date, classify_status, classify_detail
                  from wb.daily_job_stages
                 order by run_date desc limit 14
            """)
        )
        return {
            "month": month_start.isoformat(),
            "limit_usd": "100.00",
            "committed_usd": str(committed),
            "remaining_usd": str(Decimal("100.00") - Decimal(str(committed))),
            "pending_text": int(counts.pending_text),
            "pending_blank": int(counts.pending_blank),
            "claimed_without_labels": int(counts.claimed),
            "unlabeled_claims": [dict(row) for row in unresolved.mappings()],
            "classify_runs": [dict(row) for row in runs.mappings()],
            "requests": rows,
        }

    @app.get("/api/moderation/candidates")
    async def moderation_candidates(
        _: Annotated[auth.User, Depends(admin_user)],
        session: Annotated[AsyncSession, Depends(db_session)],
        candidate_status: Literal["pending", "approved", "rejected", "merged"] | None = None,
    ) -> list[dict[str, object]]:
        candidates = await DiscoveryRepository(session).candidates(status=candidate_status)
        found: list[dict[str, object]] = []
        for candidate in candidates:
            snippets = await session.execute(
                text("""
                    select concat_ws(E'\n', nullif(f.text, ''), nullif(f.pros, ''),
                                     nullif(f.cons, '')) as body
                      from wb.discovery_candidate_reviews dcr
                      join wb.feedbacks f on f.id = dcr.feedback_id
                     where dcr.candidate_id = :candidate_id
                     order by f.created_at, f.id limit 3
                """),
                {"candidate_id": candidate.id},
            )
            found.append(
                {
                    **asdict(candidate),
                    "examples": [sanitize(str(row.body or ""))[:300] for row in snippets.all()],
                }
            )
        return found

    @app.patch("/api/moderation/candidates/{candidate_id}/name")
    async def rename_candidate(
        candidate_id: int,
        payload: CandidateName,
        _: Annotated[auth.User, Depends(admin_user)],
        session: Annotated[AsyncSession, Depends(db_session)],
    ) -> dict[str, str]:
        try:
            await DiscoveryRepository(session).rename(candidate_id, payload.name)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        await session.commit()
        return {"status": "renamed"}

    @app.post("/api/moderation/candidates/{candidate_id}/approve")
    async def approve_candidate(
        candidate_id: int,
        user: Annotated[auth.User, Depends(admin_user)],
        session: Annotated[AsyncSession, Depends(db_session)],
    ) -> dict[str, int]:
        try:
            reason_id = await DiscoveryRepository(session).approve(
                candidate_id, approved_by=user.username
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, "Такая причина уже есть") from exc
        await session.commit()
        return {"reason_id": reason_id}

    @app.post("/api/moderation/candidates/{candidate_id}/merge")
    async def merge_candidate(
        candidate_id: int,
        payload: CandidateMerge,
        _: Annotated[auth.User, Depends(admin_user)],
        session: Annotated[AsyncSession, Depends(db_session)],
    ) -> dict[str, str]:
        try:
            await DiscoveryRepository(session).merge(candidate_id, payload.target_id)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        await session.commit()
        return {"status": "merged"}

    @app.post("/api/moderation/candidates/{candidate_id}/reject")
    async def reject_candidate(
        candidate_id: int,
        _: Annotated[auth.User, Depends(admin_user)],
        session: Annotated[AsyncSession, Depends(db_session)],
    ) -> dict[str, str]:
        try:
            await DiscoveryRepository(session).reject(candidate_id)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        await session.commit()
        return {"status": "rejected"}

    @app.get("/api/attention")
    async def attention_list(
        _: Annotated[auth.User, Depends(current_user)],
        session: Annotated[AsyncSession, Depends(db_session)],
        filters: Annotated[ProductFilters, Depends()],
        nm_id: Annotated[list[int] | None, Query()] = None,
        since: dt.date | None = None,
        until: dt.date | None = None,
        limit: int = Query(default=20, ge=1, le=100),
    ) -> list[dict[str, object]]:
        first_day = since or dt.datetime.now(settings.period.tz).date() - dt.timedelta(days=14)
        last_day = until or dt.datetime.now(settings.period.tz).date()
        if last_day < first_day:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Конец периода раньше начала")
        ids = await select_product_ids(session, filters, nm_ids=nm_id)
        return await attention(
            session, since=first_day, until=last_day, query=None, limit=limit, nm_ids=ids
        )

    @app.get("/api/signals")
    async def signals(
        _: Annotated[auth.User, Depends(current_user)],
        session: Annotated[AsyncSession, Depends(db_session)],
        filters: Annotated[ProductFilters, Depends()],
        nm_id: Annotated[list[int] | None, Query()] = None,
        signal_type: str | None = None,
        since: dt.date | None = None,
        until: dt.date | None = None,
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, object]]:
        ids = await select_product_ids(session, filters, nm_ids=nm_id)
        return await signal_history(
            session,
            nm_id=None,
            signal_type=signal_type,
            query=None,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
            nm_ids=ids,
        )

    @app.get("/api/products/{nm_id}/snapshots")
    async def product_history(
        nm_id: int,
        _: Annotated[auth.User, Depends(current_user)],
        session: Annotated[AsyncSession, Depends(db_session)],
        start: dt.date,
        end: dt.date,
    ) -> dict[str, object]:
        try:
            product = await product_snapshots(session, nm_id=nm_id, start=start, end=end)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        if product is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Артикул не найден")
        return product

    @app.get("/api/signal-settings")
    async def all_signal_settings(
        _: Annotated[auth.User, Depends(current_user)],
        session: Annotated[AsyncSession, Depends(db_session)],
    ) -> list[dict[str, object]]:
        settings_by_type = await SignalRepository(session).settings()
        return [asdict(setting) for setting in settings_by_type.values()]

    @app.get("/api/signal-settings/{signal_type}")
    async def signal_setting(
        signal_type: str,
        _: Annotated[auth.User, Depends(current_user)],
        session: Annotated[AsyncSession, Depends(db_session)],
    ) -> dict[str, object]:
        setting = await get_setting(session, signal_type)
        if setting is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Правило не найдено")
        return asdict(setting)

    @app.patch("/api/signal-settings/{signal_type}")
    async def change_signal_setting(
        signal_type: str,
        payload: SignalSettingPatch,
        user: Annotated[auth.User, Depends(admin_user)],
        session: Annotated[AsyncSession, Depends(db_session)],
    ) -> dict[str, object]:
        changes = payload.model_dump(exclude_unset=True)
        if not changes or any(value is None for value in changes.values()):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Укажите изменяемые поля")
        setting = await update_setting(session, signal_type, changes, changed_by=user.id)
        if setting is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Правило не найдено")
        await session.commit()
        return asdict(setting)

    @app.get("/api/reports/reasons")
    async def reasons(
        _: Annotated[auth.User, Depends(current_user)],
        session: Annotated[AsyncSession, Depends(db_session)],
        start: dt.date,
        end: dt.date,
        filters: Annotated[ProductFilters, Depends()],
        nm_id: Annotated[list[int] | None, Query()] = None,
    ) -> dict[str, object]:
        try:
            ids = await select_product_ids(session, filters, nm_ids=nm_id)
            report = await load_reason_report(session, start=start, end=end, nm_ids=ids)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        return asdict(report)

    @app.get("/api/exports/ratings")
    async def export_ratings(
        _: Annotated[auth.User, Depends(current_user)],
        session: Annotated[AsyncSession, Depends(db_session)],
        start: dt.date,
        end: dt.date,
        filters: Annotated[ProductFilters, Depends()],
        nm_id: Annotated[list[int] | None, Query()] = None,
    ) -> Response:
        try:
            ids = await select_product_ids(session, filters, nm_ids=nm_id)
            report = await load_rating_report(
                session, start=start, end=end, nm_ids=ids, include_daily=True
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        return Response(
            make_rating_workbook(report),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f'attachment; filename="ratings_{start}_{end}.xlsx"',
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/exports/reasons")
    async def export_reasons(
        _: Annotated[auth.User, Depends(current_user)],
        session: Annotated[AsyncSession, Depends(db_session)],
        start: dt.date,
        end: dt.date,
        filters: Annotated[ProductFilters, Depends()],
        nm_id: Annotated[list[int] | None, Query()] = None,
    ) -> Response:
        try:
            ids = await select_product_ids(session, filters, nm_ids=nm_id)
            report = await load_reason_report(session, start=start, end=end, nm_ids=ids)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        return Response(
            make_reason_workbook(report),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f'attachment; filename="reasons_{start}_{end}.xlsx"',
                "Cache-Control": "no-store",
            },
        )

    return app
