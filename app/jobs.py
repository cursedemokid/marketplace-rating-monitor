"""Конвейеры, которые крутит планировщик. Каждый запускается и руками.

    uv run python -m app collect --date DemoPhone

Все шаги идемпотентны: повторный запуск за ту же дату обновляет данные,
а не создаёт вторые (FR-1.3). Это спасает при разборах «почему вчера
не было сигнала».
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from decimal import Decimal
from io import StringIO
from pathlib import Path

import httpx
import uvicorn
from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.llm.classifier import ChatCompletionClassifier
from app.adapters.messenger.base import NotificationChannel
from app.adapters.messenger.channels import (
    TelegramChannel,
    YandexMessengerChannel,
)
from app.adapters.messenger.yandex import YandexMessengerBot
from app.adapters.wb.client import WbAuthError, WbClient
from app.adapters.wb.token import require_categories
from app.api.main import create_app
from app.clock import Clock
from app.config import ConfigError, Secrets, Settings
from app.db.engine import build_engine
from app.db.locking import DAILY_LOCK, try_job_lock
from app.db.session import build_session_factory
from app.repositories.ai_cost import (
    MONTHLY_PROJECT_CAP_USD,
    CostLimitExceededError,
    calendar_month_start,
)
from app.repositories.ai_naming import AiNamingRepository
from app.repositories.reasons import ReasonRepository
from app.repositories.reporting import load_rating_report, load_reason_report
from app.repositories.runs import RunRepository
from app.repositories.signals import SignalRepository
from app.services import ai_naming as ai_naming_service
from app.services import analyze as analyze_service
from app.services import auth as auth_service
from app.services import classify as classify_service
from app.services import collect as collect_service
from app.services import discover as discover_service
from app.services import health as health_service
from app.services import notify as notify_service
from app.services.ai_backlog import ReserveEstimate, estimate_reserve
from app.services.ai_cost import CLASSIFIER_SYSTEM_PROMPT, ModelTariff
from app.services.ai_pricing import verify_current_a6_tariff
from app.services.nomenclature import NomenclatureService
from app.services.quality import (
    QualityMetrics,
    combine_proposals_csv,
    combine_source_csv,
    evaluate_csv,
    import_approval_xlsx,
    make_blind_review_workbook,
    sample_csv,
)
from app.services.quality_series import SeriesResult, verify_series
from app.services.rating_workbook import make_rating_workbook
from app.services.reason_workbook import make_reason_workbook


class IncompleteCollectionError(RuntimeError):
    """Сбор оставил пробелы; анализ дня может создать ложные сигналы."""


class StaleCollectionError(RuntimeError):
    """Последний требуемый день не имеет полного завершённого сбора."""


class JobAlreadyRunningError(RuntimeError):
    """Ручной запуск пересёкся с другим сбором или анализом."""


@asynccontextmanager
async def manual_run_lock(secrets: Secrets) -> AsyncIterator[None]:
    """Не запускать ручной сбор или анализ одновременно с суточной цепочкой."""
    engine = build_engine(secrets.database_url)
    try:
        async with try_job_lock(engine, DAILY_LOCK) as acquired:
            if not acquired:
                raise JobAlreadyRunningError("Сбор или анализ уже выполняется")
            yield
    finally:
        await engine.dispose()


@dataclass(frozen=True, slots=True)
class WbProfile:
    key: str
    seller_id: str | None
    feedbacks_token: str
    content_token: str
    analytics_token: str | None


def resolve_wb_profiles(secrets: Secrets) -> tuple[WbProfile, ...]:
    """Связать токены по JWT sid, не смешивая продавцов и категории."""
    configured = (
        (
            "default",
            secrets.wb_feedbacks_token,
            secrets.wb_content_token,
            secrets.wb_analytics_token,
        ),
        (
            "DemoSeller",
            secrets.wb_feedbacks_token_DemoSeller,
            secrets.wb_content_token_DemoSeller,
            secrets.wb_analytics_token_DemoSeller,
        ),
    )
    profiles: list[WbProfile] = []
    for key, feedbacks, content, analytics in configured:
        if not any((feedbacks, content, analytics)):
            if key == "default" and secrets.wb_api_token:
                legacy_claims = require_categories(
                    secrets.wb_api_token, ("Контент", "Вопросы и отзывы")
                )
                sid = legacy_claims.get("sid")
                scope = legacy_claims["s"]
                assert isinstance(scope, int)
                profiles.append(
                    WbProfile(
                        key,
                        str(sid) if sid else None,
                        secrets.wb_api_token,
                        secrets.wb_api_token,
                        secrets.wb_api_token if scope & (1 << 2) else None,
                    )
                )
            continue
        if not feedbacks or not content:
            raise ConfigError(f"Для кабинета {key} нужны токены Вопросы и отзывы и Контент")
        tokens = [feedbacks, content, *([analytics] if analytics else [])]
        claims = [require_categories(token, ()) for token in tokens]
        sids = [claim.get("sid") for claim in claims]
        if not all(isinstance(sid, str) and sid for sid in sids):
            raise ConfigError(f"В токенах кабинета {key} отсутствует sid продавца")
        if len(set(sids)) != 1:
            raise ConfigError(f"Для {key} заданы токены разных кабинетов")
        require_categories(feedbacks, ("Вопросы и отзывы",))
        require_categories(content, ("Контент",))
        analytics_token = next(
            (
                token
                for token, claim in zip(tokens, claims, strict=True)
                if isinstance(claim["s"], int) and claim["s"] & (1 << 2)
            ),
            None,
        )
        profiles.append(WbProfile(key, str(sids[0]), feedbacks, content, analytics_token))
    if not profiles:
        raise ConfigError(
            "Не заданы токены WB: нужны WbFeedbacksToken и WbContentToken для каждого кабинета"
        )
    split_tokens = any(
        (
            secrets.wb_feedbacks_token,
            secrets.wb_content_token,
            secrets.wb_feedbacks_token_DemoSeller,
            secrets.wb_content_token_DemoSeller,
        )
    )
    if split_tokens and {profile.key for profile in profiles} != {"default", "DemoSeller"}:
        raise ConfigError("Для сбора двух кабинетов нужны токены default и DemoSeller")
    if len(profiles) > 1 and any(profile.seller_id is None for profile in profiles):
        raise ConfigError("Для двух кабинетов в каждом WB-токене нужен sid продавца")
    seller_ids = [profile.seller_id for profile in profiles if profile.seller_id]
    if len(seller_ids) != len(set(seller_ids)):
        raise ConfigError("Кабинеты default и DemoSeller имеют одинаковый sid продавца")
    return tuple(profiles)


async def add_user(
    settings: Settings, secrets: Secrets, *, username: str, REDACTED: str, role: auth_service.Role
) -> auth_service.User:
    """Создать первую локальную учётку с консоли или следующую с правами на сервер."""
    engine = build_engine(secrets.database_url)
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            user = await auth_service.create_user(
                session, username=username, REDACTED=REDACTED, role=role, policy=settings.auth
            )
            await session.commit()
        logger.info("Создан пользователь {} с ролью {}", user.username, user.role)
        return user
    finally:
        await engine.dispose()


async def serve_api(settings: Settings, secrets: Secrets) -> None:
    """API слушает только адрес из конфига; типовой вариант — локальный прокси."""
    engine = build_engine(secrets.database_url)
    factory = build_session_factory(engine)
    try:
        app = create_app(factory, settings)
        server = uvicorn.Server(
            uvicorn.Config(app, host=settings.api.host, port=settings.api.port, log_level="info")
        )
        await server.serve()
    finally:
        await engine.dispose()


async def health(settings: Settings, secrets: Secrets, clock: Clock) -> None:
    """Код выхода 1 при пропущенном или частичном суточном сборе."""
    engine = build_engine(secrets.database_url)
    session_factory = build_session_factory(engine)
    try:
        has_tokens = any(
            (
                secrets.wb_api_token,
                secrets.wb_feedbacks_token,
                secrets.wb_content_token,
                secrets.wb_feedbacks_token_DemoSeller,
                secrets.wb_content_token_DemoSeller,
            )
        )
        accounts = (
            tuple(profile.key for profile in resolve_wb_profiles(secrets))
            if has_tokens
            else ("default",)
        )
        async with session_factory() as session:
            state = await health_service.check_collection(
                session,
                clock,
                run_at=settings.collector.run_at,
                grace_hours=settings.collector.health_grace_hours,
                account_keys=accounts,
            )
        if not state.healthy:
            msg = f"Нет полного сбора со снимками за {state.expected_date}"
            raise StaleCollectionError(msg)
        logger.info("Сбор за {} проверен", state.expected_date)
    finally:
        await engine.dispose()


async def history(secrets: Secrets, on_date: dt.date) -> None:
    """Показать полноту непрерывной истории по каждому кабинету."""
    account_keys = tuple(profile.key for profile in resolve_wb_profiles(secrets))
    engine = build_engine(secrets.database_url)
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            settings = await SignalRepository(session).settings()
            baseline_days = max(
                (item.baseline_days for item in settings.values() if item.is_enabled),
                default=14,
            )
            coverage = await health_service.history_coverage(
                session,
                on_date=on_date,
                baseline_days=baseline_days,
                account_keys=account_keys,
            )
        for item in coverage:
            logger.info(
                "История на {}: кабинет {}, полная у {}/{} товаров ({} снимков на товар)",
                on_date,
                item.account_key,
                item.ready,
                item.monitored,
                item.required_days,
            )
    finally:
        await engine.dispose()


async def export_ratings(
    secrets: Secrets,
    *,
    start: dt.date,
    end: dt.date,
    nm_ids: list[int] | None,
    output: Path,
) -> int:
    """Сформировать Excel по одной выборке для всех выбранных артикулов."""
    engine = build_engine(secrets.database_url)
    session_factory = build_session_factory(engine)
    try:
        async with session_factory() as session:
            report = await load_rating_report(
                session, start=start, end=end, nm_ids=nm_ids, include_daily=True
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(make_rating_workbook(report))
        logger.info("Отчёт за {}—{}: {} артикулов, файл {}", start, end, len(report.rows), output)
        return len(report.rows)
    finally:
        await engine.dispose()


async def export_reasons(
    secrets: Secrets,
    *,
    start: dt.date,
    end: dt.date,
    nm_ids: list[int] | None,
    output: Path,
) -> int:
    """Сформировать Excel-матрицу причин по выбранным артикулам и датам."""
    engine = build_engine(secrets.database_url)
    session_factory = build_session_factory(engine)
    try:
        async with session_factory() as session:
            report = await load_reason_report(session, start=start, end=end, nm_ids=nm_ids)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(make_reason_workbook(report))
        logger.info("Отчёт причин за {}—{}: файл {}", start, end, output)
        return len(report.rows)
    finally:
        await engine.dispose()


def _verified_tariff(settings: Settings, secrets: Secrets) -> ModelTariff:
    ai = settings.ai
    if not secrets.llm_api_key or not secrets.llm_base_url or not secrets.llm_model_primary:
        raise ConfigError("Для AI задайте LLM_API_KEY, LLM_BASE_URL и LLM_MODEL_PRIMARY")
    if (
        ai.input_usd_per_1m is None
        or ai.output_usd_per_1m is None
        or not ai.tariff_source_url
        or ai.tariff_checked_on is None
    ):
        raise ConfigError("Тариф A6API не подтверждён: заполните цены, источник и дату в ai")
    return ModelTariff(
        secrets.llm_model_primary,
        Decimal(str(ai.input_usd_per_1m)),
        Decimal(str(ai.output_usd_per_1m)),
    )


def _feedback_ids_from_csv(input_csv: Path | None) -> list[str] | None:
    if input_csv is None:
        return None
    reader = csv.DictReader(StringIO(input_csv.read_text(encoding="utf-8-sig")))
    if reader.fieldnames is None or "feedback_id" not in reader.fieldnames:
        raise ConfigError("CSV должен содержать колонку feedback_id")
    values = [str(row["feedback_id"] or "").strip() for row in reader]
    if not values or not all(values) or len(values) != len(set(values)):
        raise ConfigError("feedback_id в CSV должны быть непустыми и уникальными")
    return values


async def classify(
    settings: Settings,
    secrets: Secrets,
    clock: Clock,
    *,
    until: dt.date,
    limit: int,
    input_csv: Path | None = None,
    allow_uncertain_manual: bool = False,
    retry: bool = False,
) -> classify_service.ClassifyResult:
    """Ограниченный ручной или автоматический прогон с проектным бюджетом."""
    if limit < 1:
        raise ConfigError("Для classify нужен положительный --limit")
    if retry and input_csv is None:
        raise ConfigError("Для повторной оплаты нужен явный CSV со списком ID")
    selected_ids = _feedback_ids_from_csv(input_csv)
    tariff = _verified_tariff(settings, secrets)

    async def check_tariff() -> None:
        await verify_current_a6_tariff(
            tariff, pricing_url=settings.ai.tariff_source_url or "", api_key=secrets.llm_api_key
        )

    engine = build_engine(secrets.database_url)
    factory = build_session_factory(engine)
    try:
        async with (
            factory() as session,
            ChatCompletionClassifier(
                base_url=secrets.llm_base_url,
                api_key=secrets.llm_api_key,
                model=secrets.llm_model_primary,
                tariff=tariff,
                max_output_tokens=settings.ai.max_output_tokens,
                timeout_seconds=settings.ai.request_timeout_seconds,
            ) as classifier,
        ):
            result = await classify_service.run(
                session,
                classifier,
                clock,
                until=until,
                batch_size=settings.ai.batch_size,
                max_stars=settings.ai.classify_stars_max,
                limit=limit,
                feedback_ids=None if retry else selected_ids,
                retry_feedback_ids=selected_ids if retry else None,
                max_input_tokens=settings.ai.max_input_tokens,
                max_output_tokens=settings.ai.max_output_tokens,
                allow_uncertain_manual=allow_uncertain_manual,
                before_paid_request=check_tariff,
            )
            logger.info(
                "AI классифицировал {} отзывов за {} вызовов",
                result.classified,
                result.batches,
            )
            return result
    finally:
        await engine.dispose()


@dataclass(frozen=True, slots=True)
class ClassifyPlan:
    pending_text: int
    pending_blank: int
    claimed_without_labels: int
    selected: ReserveEstimate
    committed_usd: Decimal
    remaining_usd: Decimal


async def classify_plan(
    settings: Settings,
    secrets: Secrets,
    *,
    until: dt.date,
    limit: int,
    input_csv: Path | None = None,
    retry: bool = False,
) -> ClassifyPlan:
    """Estimate bounded reserve without sending a paid request or writing data."""
    if limit < 1:
        raise ConfigError("Для плана classify нужен положительный --limit")
    if retry and input_csv is None:
        raise ConfigError("Для плана повтора нужен --input CSV с feedback_id")
    selected_ids = _feedback_ids_from_csv(input_csv)
    tariff = _verified_tariff(settings, secrets)
    engine = build_engine(secrets.database_url)
    try:
        async with build_session_factory(engine)() as session:
            repo = ReasonRepository(session)
            if retry:
                assert selected_ids is not None
                selected = await repo.pending_retry(
                    until=until,
                    max_stars=settings.ai.classify_stars_max,
                    limit=limit,
                    feedback_ids=selected_ids,
                )
                if len(selected) != min(len(selected_ids), limit):
                    raise ConfigError("Не все указанные отзывы доступны для единственного повтора")
            else:
                selected = await repo.pending(
                    until=until,
                    max_stars=settings.ai.classify_stars_max,
                    limit=limit,
                    feedback_ids=selected_ids,
                )
            estimate = estimate_reserve(
                selected,
                await repo.approved(),
                tariff=tariff,
                batch_size=settings.ai.batch_size,
                max_output_tokens=settings.ai.max_output_tokens,
                max_input_tokens=settings.ai.max_input_tokens,
            )
            counts = await session.execute(
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
                    where f.created_date <= :until and f.valuation <= :stars
                      and not exists (
                        select 1 from wb.feedback_reasons fr where fr.feedback_id = f.id
                      )
                """),
                {"until": until, "stars": settings.ai.classify_stars_max},
            )
            row = counts.one()
            month_start = calendar_month_start(dt.datetime.now(dt.UTC))
            spent = await session.execute(
                text("""
                    select coalesce(sum(case when status = 'settled' then actual_cost_usd
                                             else estimated_cost_usd end), 0)
                      from wb.ai_cost_requests where month_start = :month_start
                """),
                {"month_start": month_start},
            )
            committed = Decimal(str(spent.scalar_one()))
            plan = ClassifyPlan(
                int(row.pending_text),
                int(row.pending_blank),
                int(row.claimed),
                estimate,
                committed,
                MONTHLY_PROJECT_CAP_USD - committed,
            )
        logger.info(
            "AI-план: в очереди с текстом {}, пустых {}, закреплено без меток {}; "
            "выбрано {}, из них платных {} за {} пакетов, максимум ${}, "
            "за месяц учтено ${}, доступно ${}",
            plan.pending_text,
            plan.pending_blank,
            plan.claimed_without_labels,
            plan.selected.attempted,
            plan.selected.paid_reviews,
            plan.selected.paid_batches,
            plan.selected.max_cost_usd,
            plan.committed_usd,
            plan.remaining_usd,
        )
        return plan
    finally:
        await engine.dispose()


async def import_nomenclature(secrets: Secrets, *, path: Path, confirm: bool) -> int:
    """Сверить файл; запись выполняется только при явном подтверждении."""
    content = path.read_bytes()
    engine = build_engine(secrets.database_url)
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            service = NomenclatureService(session)
            preview = await service.preview_import_xlsx(content)
            logger.info(
                "Номенклатура: строк {}, готовы {}, ошибок {}, повторов {}, нет в WB {}, "
                "расхождений {}, нет в файле {}",
                len(preview.rows),
                preview.valid_count,
                len(preview.row_errors),
                len(preview.duplicates),
                len(preview.unknown_products),
                len(preview.discrepancies),
                len(preview.missing_in_file),
            )
            for row_error in preview.row_errors:
                logger.warning("Строка {}: {}", row_error.row_number, row_error.message)
            for row_issue in (
                *preview.duplicates,
                *preview.unknown_products,
                *preview.discrepancies,
            ):
                logger.warning(
                    "Строка {}: {} {}",
                    row_issue.row_number,
                    row_issue.vendor_code,
                    row_issue.message,
                )
            for row_issue in preview.missing_in_file[:10]:
                logger.warning("Нет в XLSX: {} / {}", row_issue.account_key, row_issue.vendor_code)
            if len(preview.missing_in_file) > 10:
                logger.warning(
                    "Ещё {} артикулов нет в XLSX; полный список есть в API",
                    len(preview.missing_in_file) - 10,
                )
            if not confirm:
                return preview.valid_count
            count = await service.confirm_import_xlsx(content)
            await session.commit()
            logger.info("Загружено {} товаров", count)
            return count
    finally:
        await engine.dispose()


async def quality_sample(
    secrets: Secrets,
    *,
    count: int,
    output: Path,
    exclude: Path | None = None,
    unclassified_only: bool = False,
) -> None:
    exclude_ids: set[str] = set()
    if exclude is not None:
        reader = csv.DictReader(StringIO(exclude.read_text(encoding="utf-8-sig")))
        if reader.fieldnames is None or "feedback_id" not in reader.fieldnames:
            raise ConfigError("Файл исключений должен содержать колонку feedback_id")
        values = [str(row["feedback_id"] or "").strip() for row in reader]
        if not values or not all(values) or len(values) != len(set(values)):
            raise ConfigError("feedback_id в файле исключений должны быть непустыми и уникальными")
        exclude_ids = set(values)
    engine = build_engine(secrets.database_url)
    try:
        selected_at = dt.datetime.now(dt.UTC)
        async with build_session_factory(engine)() as session:
            sample, catalog = await sample_csv(
                session,
                count=count,
                exclude_ids=exclude_ids,
                unclassified_only=unclassified_only,
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("\ufeff" + sample, encoding="utf-8")
        catalog_path = output.with_name(output.stem + "_reasons.csv")
        catalog_path.write_text("\ufeff" + catalog, encoding="utf-8")
        manifest = {
            "schema_version": 1,
            "selected_at": selected_at.isoformat(),
            "count": count,
            "unclassified_only": unclassified_only,
            "model": secrets.llm_model_primary or None,
            "prompt_sha256": hashlib.sha256(CLASSIFIER_SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
            "sample_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "catalog_sha256": hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
        }
        output.with_name(output.stem + ".manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        logger.info("Подготовлено {} отзывов для ручной разметки: {}", count, output)
    finally:
        await engine.dispose()


async def _quality_catalog_csv(session: AsyncSession) -> str:
    reasons = await ReasonRepository(session).approved()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["reason_id", "reason_name"])
    writer.writerows(sorted(reasons.items()))
    return output.getvalue()


async def quality_review(secrets: Secrets, *, source: Path, output: Path) -> None:
    """Create a review book without the model predictions or provisional scores."""
    reviews = combine_source_csv([source])
    engine = build_engine(secrets.database_url)
    try:
        async with build_session_factory(engine)() as session:
            catalog = await _quality_catalog_csv(session)
        workbook = make_blind_review_workbook(reviews, catalog)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(workbook)
        logger.info("Книга для проверки создана: {}", output)
    finally:
        await engine.dispose()


async def quality_approve(
    secrets: Secrets,
    *,
    workbook: Path,
    source: Path | None,
    reviewer: str,
    output_dir: Path,
) -> QualityMetrics:
    """Validate every reviewed row and save reproducible gold and audit files."""
    if output_dir.exists():
        raise ConfigError(f"Каталог результатов уже существует: {output_dir}")
    if source is None:
        root = Path(__file__).resolve().parents[1]
        paths = [
            root / "outputs/quality_current_DemoPhone/sample_100.csv",
            root / "outputs/quality_current_DemoPhone/sample_extra_20.csv",
        ]
        proposal_paths = [
            root / "outputs/quality_current_DemoPhone/first_pass_100.csv",
            root / "outputs/quality_current_DemoPhone/first_pass_extra_20.csv",
        ]
    else:
        paths = [source]
        proposal_paths = []
    reviews = combine_source_csv(paths)
    proposals = combine_proposals_csv(proposal_paths) if proposal_paths else None
    engine = build_engine(secrets.database_url)
    try:
        async with build_session_factory(engine)() as session:
            catalog = await _quality_catalog_csv(session)
            approved = import_approval_xlsx(
                workbook.read_bytes(),
                source_reviews_csv=reviews,
                catalog_csv=catalog,
                reviewer=reviewer,
                classifier_model=secrets.llm_model_primary or None,
                source_proposals_csv=proposals,
            )
            metrics = await evaluate_csv(session, approved.evaluation_csv)
        output_dir.mkdir(parents=True, exist_ok=False)
        (output_dir / "gold.csv").write_text(approved.evaluation_csv, encoding="utf-8")
        (output_dir / "decisions.csv").write_text(approved.audit_csv, encoding="utf-8")
        (output_dir / "manifest.json").write_text(approved.manifest_json, encoding="utf-8")
        report = {**asdict(metrics), "f1_gate_passed": metrics.f1 >= 0.75}
        (output_dir / "metrics.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        logger.info("Утверждено {} отзывов, micro-F1={:.4f}", metrics.reviews, metrics.f1)
        return metrics
    finally:
        await engine.dispose()


async def quality_verify(
    settings: Settings, secrets: Secrets, *, sample: Path, output: Path
) -> SeriesResult:
    """Verify the fixed 100-review first-attempt series against the database."""
    sample_bytes = sample.read_bytes()
    manifest_path = sample.with_name(sample.stem + ".manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("Манифест контрольной серии должен быть JSON-объектом")
    if hashlib.sha256(sample_bytes).hexdigest() != manifest.get("sample_sha256"):
        raise ValueError("Файл контрольной серии изменён после формирования манифеста")
    if manifest.get("count") != 100 or manifest.get("unclassified_only") is not True:
        raise ValueError("Нужна новая серия из 100 ещё не классифицированных отзывов")
    if manifest.get("model") != secrets.llm_model_primary:
        raise ValueError("Модель серии не совпадает с текущей моделью классификатора")
    prompt_sha256 = hashlib.sha256(CLASSIFIER_SYSTEM_PROMPT.encode("utf-8")).hexdigest()
    if manifest.get("prompt_sha256") != prompt_sha256:
        raise ValueError("Промпт изменился после отбора контрольной серии")
    try:
        selected_at = dt.datetime.fromisoformat(str(manifest["selected_at"]))
    except (KeyError, ValueError) as exc:
        raise ValueError("В манифесте нет корректного времени отбора") from exc
    engine = build_engine(secrets.database_url)
    try:
        async with build_session_factory(engine)() as session:
            result = await verify_series(
                session,
                sample_bytes.decode("utf-8-sig"),
                selected_at=selected_at,
                model=secrets.llm_model_primary,
                prompt_sha256=prompt_sha256,
                batch_size=settings.ai.batch_size,
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        report = asdict(result)
        report["selected_at"] = selected_at.isoformat()
        report["sample_sha256"] = manifest["sample_sha256"]
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        logger.info(
            "Первая попытка AI: {}/{} отзывов; порог 95/100 {}",
            result.first_attempt_success,
            result.total,
            "пройден" if result.pass_gate else "не пройден",
        )
        return result
    finally:
        await engine.dispose()


async def quality_evaluate(secrets: Secrets, *, path: Path) -> QualityMetrics:
    engine = build_engine(secrets.database_url)
    try:
        async with build_session_factory(engine)() as session:
            metrics = await evaluate_csv(session, path.read_text(encoding="utf-8-sig"))
        logger.info(
            "Качество на {} отзывах: precision={:.3f}, recall={:.3f}, F1={:.3f}, exact={:.3f}",
            metrics.reviews,
            metrics.precision,
            metrics.recall,
            metrics.f1,
            metrics.exact_match,
        )
        return metrics
    finally:
        await engine.dispose()


async def collect(
    settings: Settings, secrets: Secrets, clock: Clock, run_date: dt.date
) -> collect_service.CollectResult:
    """Сбор данных из Wildberries за дату. TSK-107."""
    profiles = resolve_wb_profiles(secrets)

    engine = build_engine(secrets.database_url)
    session_factory = build_session_factory(engine)
    try:
        results: list[collect_service.CollectResult] = []
        for profile in profiles:
            async with (
                WbClient(
                    profile.feedbacks_token,
                    content_token=profile.content_token,
                    analytics_token=profile.analytics_token,
                    sandbox=secrets.wb_use_sandbox,
                ) as client,
                session_factory() as session,
            ):
                try:
                    results.append(
                        await collect_service.run(
                            session,
                            client,
                            clock,
                            run_date=run_date,
                            recheck_days=settings.collector.recheck_days,
                            account_key=profile.key,
                            analytics_available=profile.analytics_token is not None,
                        )
                    )
                except WbAuthError:
                    logger.warning(
                        "Кабинет {}: WB отклонил токен (401); продолжаем следующий", profile.key
                    )
                    results.append(
                        collect_service.CollectResult(
                            products=0,
                            feedbacks=0,
                            skipped_feedbacks=0,
                            snapshots=0,
                            rechecked_dates=0,
                            errors=(f"{profile.key}: авторизация WB 401",),
                        )
                    )
        result = collect_service.CollectResult(
            products=sum(item.products for item in results),
            feedbacks=sum(item.feedbacks for item in results),
            skipped_feedbacks=sum(item.skipped_feedbacks for item in results),
            snapshots=sum(item.snapshots for item in results),
            rechecked_dates=sum(item.rechecked_dates for item in results),
            failed_products=sum(item.failed_products for item in results),
            errors=tuple(error for item in results for error in item.errors),
        )
        logger.info(
            "Прогон за {} завершён: товаров {}, отзывов {}, снимков {}, "
            "перепроверено прошлых дней {}",
            run_date,
            result.products,
            result.feedbacks,
            result.snapshots,
            result.rechecked_dates,
        )
        return result
    finally:
        await engine.dispose()


async def analyze(secrets: Secrets, clock: Clock, run_date: dt.date) -> None:
    """Дельты, значимость, правила сигналов. TSK-201…TSK-205.

    Wildberries здесь не дёргается вовсе: считаем по уже собранным снимкам,
    поэтому прогон дешёвый и его можно гонять сколько угодно раз.
    """
    engine = build_engine(secrets.database_url)
    session_factory = build_session_factory(engine)
    try:
        async with session_factory() as session:
            result = await analyze_service.run(session, clock, run_date=run_date)
            await session.commit()
        logger.info(
            "Анализ за {}: артикулов {}, сигналов {}, подавлено {}, без базы сравнения {}",
            run_date,
            result.products,
            result.created,
            result.suppressed,
            result.skipped_no_data,
        )
    finally:
        await engine.dispose()


def build_channel(
    settings: Settings,
    secrets: Secrets,
) -> tuple[NotificationChannel | None, object | None]:
    """Канал доставки по настройке. FR-4.2.

    Второй элемент — то, что нужно закрыть после отправки (клиенты httpx).
    `dry_run` перебивает выбор канала: в этом режиме наружу не уходит
    ничего, что бы ни стояло в `notify.channel` (FR-4.4, TSK-305).
    """
    if settings.notify.dry_run:
        logger.info("notify.dry_run включён: сигналы считаются, но в чат не уходят")
        return None, None

    if settings.notify.channel == "yandex_messenger":
        if not (secrets.yandex_bot_oauth_token and secrets.yandex_bot_chat_id):
            msg = (
                "Канал yandex_messenger выбран, но нет YANDEX_BOT_OAUTH_TOKEN "
                "или YANDEX_BOT_CHAT_ID — см. docs/03_КЛЮЧИ_И_ДОСТУПЫ.md § 2"
            )
            raise ConfigError(msg)
        bot = YandexMessengerBot(secrets.yandex_bot_oauth_token)
        return YandexMessengerChannel(bot, secrets.yandex_bot_chat_id), bot

    if settings.notify.channel == "telegram":
        if not (secrets.telegram_bot_token and secrets.telegram_chat_id):
            msg = "Канал telegram выбран, но нет TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID"
            raise ConfigError(msg)
        channel = TelegramChannel(secrets.telegram_bot_token, secrets.telegram_chat_id)
        return channel, channel


async def notify(settings: Settings, secrets: Secrets, clock: Clock) -> notify_service.NotifyResult:
    """Доставка неотправленных сигналов. TSK-301…TSK-305."""
    channel, closeable = build_channel(settings, secrets)
    engine = build_engine(secrets.database_url)
    session_factory = build_session_factory(engine)
    try:
        async with session_factory() as session:
            result = await notify_service.run(
                session, clock, channel, dry_run=settings.notify.dry_run
            )
            await session.commit()
        logger.info(
            "Доставка завершена: отправлено {}, не удалось {}, пробных {}",
            result.sent,
            result.failed,
            result.simulated,
        )
        return result
    finally:
        if closeable is not None and hasattr(closeable, "aclose"):
            await closeable.aclose()
        await engine.dispose()


async def daily_for_date(
    settings: Settings, secrets: Secrets, clock: Clock, run_date: dt.date
) -> bool:
    """Последовательно обработать дату; False означает занятую блокировку."""
    engine = build_engine(secrets.database_url)
    try:
        async with try_job_lock(engine, DAILY_LOCK) as acquired:
            if not acquired:
                logger.warning("Ежедневный прогон за {} уже выполняется другим процессом", run_date)
                return False
            logger.info("Ежедневный прогон за {} начат", run_date)
            async with build_session_factory(engine)() as session:
                repo = RunRepository(session)
                stages = await repo.get_daily_stages(run_date)
            if not stages["collect"]:
                result = await collect(settings, secrets, clock, run_date)
                if result.errors or result.skipped_feedbacks or result.failed_products:
                    msg = (
                        f"Сбор за {run_date} неполный: артикулов с ошибками "
                        f"{result.failed_products}, пропущено отзывов {result.skipped_feedbacks}. "
                        f"Повторите сбор; анализ дня не запущен."
                    )
                    raise IncompleteCollectionError(msg)
                async with build_session_factory(engine)() as session:
                    await RunRepository(session).mark_daily_stage(run_date, "collect")
                    await session.commit()
                stages["collect"] = True
            if not stages["classify"]:
                if settings.ai.automatic_enabled:
                    ai_status = "complete"
                    ai_detail: str | None = None
                    try:
                        ai_result = await classify(
                            settings,
                            secrets,
                            clock,
                            until=run_date,
                            limit=settings.ai.daily_limit,
                        )
                        if ai_result.stopped_due_provider:
                            ai_status = "deferred_provider"
                        elif getattr(ai_result, "stopped_due_budget", False):
                            ai_status = "deferred_budget"
                        elif ai_result.deferred:
                            ai_status = "deferred_limit"
                        elif ai_result.failed:
                            ai_status = "partial"
                        if ai_status != "complete":
                            ai_detail = (
                                f"attempted={ai_result.attempted}, "
                                f"classified={ai_result.classified}, failed={ai_result.failed}, "
                                f"deferred={ai_result.deferred}"
                            )
                    except CostLimitExceededError as exc:
                        ai_status, ai_detail = "deferred_budget", str(exc)
                    except (
                        ConfigError,
                        classify_service.ClassificationError,
                        httpx.HTTPError,
                    ) as exc:
                        ai_status, ai_detail = "deferred_config", str(exc)
                    if ai_detail:
                        logger.warning("AI-классификация за {} отложена: {}", run_date, ai_detail)
                else:
                    ai_status, ai_detail = "disabled", "AI выключен до приёмки качества"
                async with build_session_factory(engine)() as session:
                    await RunRepository(session).record_classify_status(
                        run_date, ai_status, ai_detail
                    )
                    await session.commit()
                stages["classify"] = ai_status in {"complete", "partial", "disabled"}
            if not stages["analyze"]:
                await analyze(secrets, clock, run_date)
                async with build_session_factory(engine)() as session:
                    await RunRepository(session).mark_daily_stage(run_date, "analyze")
                    await session.commit()
                stages["analyze"] = True
            if not stages["notify"]:
                notify_result = await notify(settings, secrets, clock)
                if notify_result.failed == 0:
                    async with build_session_factory(engine)() as session:
                        await RunRepository(session).mark_daily_stage(run_date, "notify")
                        await session.commit()
            logger.info("Ежедневный прогон за {} завершён", run_date)
            return True
    finally:
        await engine.dispose()


async def daily(settings: Settings, secrets: Secrets, clock: Clock) -> None:
    """Последовательно обработать последние завершённые московские сутки."""
    await daily_for_date(settings, secrets, clock, clock.today() - dt.timedelta(days=1))


async def discover(
    settings: Settings, secrets: Secrets, *, until: dt.date, limit: int | None = None
) -> discover_service.DiscoveryResult:
    """Найти повторяющиеся темы локально и поставить кандидатов в очередь."""
    batch_limit = limit or 5000
    if batch_limit < settings.ai.discover_min_cluster_size:
        raise ConfigError("Лимит discover меньше минимального размера группы")
    engine = build_engine(secrets.database_url)
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            result = await discover_service.run(
                session,
                until=until,
                min_cluster_size=settings.ai.discover_min_cluster_size,
                batch_limit=batch_limit,
            )
            await session.commit()
            return result
    finally:
        await engine.dispose()


async def name_candidates(settings: Settings, secrets: Secrets, clock: Clock, *, limit: int) -> int:
    """Предложить названия для ожидающих групп, расходуя общий лимит AI."""
    if limit < 1:
        raise ConfigError("Для name-candidates нужен положительный --limit")
    tariff = _verified_tariff(settings, secrets)

    async def check_tariff() -> None:
        await verify_current_a6_tariff(
            tariff, pricing_url=settings.ai.tariff_source_url or "", api_key=secrets.llm_api_key
        )

    engine = build_engine(secrets.database_url)
    factory = build_session_factory(engine)
    try:
        async with (
            factory() as session,
            ChatCompletionClassifier(
                base_url=secrets.llm_base_url,
                api_key=secrets.llm_api_key,
                model=secrets.llm_model_primary,
                tariff=tariff,
                max_output_tokens=settings.ai.max_output_tokens,
                timeout_seconds=settings.ai.request_timeout_seconds,
            ) as classifier,
        ):
            ids = await AiNamingRepository(session).pending_candidate_ids(limit=limit)
            named = 0
            for candidate_id in ids:
                proposed = await ai_naming_service.propose_name(
                    session,
                    candidate_id,
                    classifier,
                    tariff,
                    clock,
                    max_input_tokens=settings.ai.max_input_tokens,
                    max_output_tokens=settings.ai.max_output_tokens,
                    before_paid_request=check_tariff,
                )
                if proposed is not None:
                    named += 1
            logger.info("AI предложил названия для {} из {} групп", named, len(ids))
            return named
    finally:
        await engine.dispose()
