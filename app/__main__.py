"""Точка входа. Один образ, разные роли — как в docs/05 § 3.

uv run python -m app check              проверить конфиг и доступы
uv run python -m app collect [--date]   сбор за дату
uv run python -m app analyze [--date]   расчёт и сигналы
uv run python -m app notify             доставка накопленных сигналов
uv run python -m app api                REST для дашборда
uv run python -m app add-user           первая локальная учётка
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import getpass
import sys
from pathlib import Path

from loguru import logger

from app import jobs
from app.adapters.wb.client import WbAuthError
from app.clock import SystemClock
from app.config import ConfigError, load_secrets, load_settings
from app.logging import setup_logging
from app.scheduler import catch_up, due_collection_date, run_forever
from app.services.auth import UserExistsError


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="app", description="Мониторинг рейтингов Wildberries")
    parser.add_argument(
        "command",
        choices=[
            "check",
            "health",
            "history",
            "collect",
            "analyze",
            "notify",
            "daily",
            "catch-up",
            "scheduler",
            "export-ratings",
            "export-reasons",
            "import-nomenclature",
            "classify",
            "discover",
            "name-candidates",
            "quality-sample",
            "quality-evaluate",
            "quality-review",
            "quality-approve",
            "quality-verify",
            "api",
            "add-user",
        ],
    )
    parser.add_argument("--date", type=dt.date.fromisoformat, default=None)
    parser.add_argument("--start", type=dt.date.fromisoformat, default=None)
    parser.add_argument("--end", type=dt.date.fromisoformat, default=None)
    parser.add_argument("--nm-id", type=int, action="append", dest="nm_ids")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--input", type=Path, default=None)
    parser.add_argument("--exclude", type=Path, default=None)
    parser.add_argument("--unclassified-only", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--until", type=dt.date.fromisoformat, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--allow-uncertain-manual", action="store_true")
    parser.add_argument("--retry", action="store_true")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--source", type=Path, default=None)
    parser.add_argument("--reviewer", default=None)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--username", default=None)
    parser.add_argument("--role", choices=["viewer", "admin"], default=None)
    return parser.parse_args(argv)


def _collection_target(requested: dt.date | None, today: dt.date) -> dt.date:
    """WB Analytics принимает завершённые дни, но отклоняет текущий день."""
    target = requested or today - dt.timedelta(days=1)
    if target >= today:
        raise ConfigError(
            "Сбор за текущий день или будущую дату невозможен: укажите вчерашнюю дату"
        )
    return target


async def _run(args: argparse.Namespace) -> None:
    settings = load_settings()
    secrets = load_secrets()
    setup_logging(sentry_dsn=secrets.sentry_dsn)

    clock = SystemClock(settings.period.tz)
    target = args.date or clock.today()

    if args.command == "check":
        if any(
            (
                secrets.wb_api_token,
                secrets.wb_feedbacks_token,
                secrets.wb_content_token,
                secrets.wb_feedbacks_token_DemoSeller,
                secrets.wb_content_token_DemoSeller,
            )
        ):
            jobs.resolve_wb_profiles(secrets)
            logger.info("Категории WB-токена проверены; клиент ограничен методами чтения")
        logger.info("Конфигурация прочитана, часовой пояс {}", settings.period.timezone)
        logger.info(
            "Канал сигналов: {}, dry_run={}",
            settings.notify.channel,
            settings.notify.dry_run,
        )
        return
    if args.command == "collect":
        target = _collection_target(args.date, clock.today())
        async with jobs.manual_run_lock(secrets):
            result = await jobs.collect(settings, secrets, clock, target)
            if result.errors or result.skipped_feedbacks or result.failed_products:
                raise jobs.IncompleteCollectionError(
                    f"Сбор за {target} сохранён частично: "
                    f"{'; '.join(result.errors) or 'есть пропуски в данных'}"
                )
    elif args.command == "health":
        await jobs.health(settings, secrets, clock)
    elif args.command == "history":
        history_date = args.date or due_collection_date(clock, settings.collector.run_at)
        await jobs.history(secrets, history_date)
    elif args.command == "analyze":
        async with jobs.manual_run_lock(secrets):
            await jobs.analyze(secrets, clock, target)
    elif args.command == "notify":
        await jobs.notify(settings, secrets, clock)
    elif args.command == "daily":
        await jobs.daily(settings, secrets, clock)
    elif args.command == "catch-up":
        await catch_up(settings, secrets, clock)
    elif args.command == "scheduler":
        await run_forever(settings, secrets, clock)
    elif args.command == "export-ratings":
        if args.start is None or args.end is None:
            raise ConfigError("Для export-ratings нужны --start и --end")
        output = args.output or Path(f"exports/ratings_{args.start}_{args.end}.xlsx")
        await jobs.export_ratings(
            secrets, start=args.start, end=args.end, nm_ids=args.nm_ids, output=output
        )
    elif args.command == "export-reasons":
        if args.start is None or args.end is None:
            raise ConfigError("Для export-reasons нужны --start и --end")
        output = args.output or Path(f"exports/reasons_{args.start}_{args.end}.xlsx")
        await jobs.export_reasons(
            secrets, start=args.start, end=args.end, nm_ids=args.nm_ids, output=output
        )
    elif args.command == "import-nomenclature":
        if args.input is None:
            raise ConfigError("Для import-nomenclature нужен --input XLSX")
        await jobs.import_nomenclature(secrets, path=args.input, confirm=args.confirm)
    elif args.command == "classify":
        if args.until is None or args.limit is None:
            raise ConfigError("Для classify нужны --until и --limit")
        if args.retry and args.input is None:
            raise ConfigError("Для платного повтора нужны --retry и --input CSV с feedback_id")
        if args.plan:
            await jobs.classify_plan(
                settings,
                secrets,
                until=args.until,
                limit=args.limit,
                input_csv=args.input,
                retry=args.retry,
            )
            return
        async with jobs.manual_run_lock(secrets):
            await jobs.classify(
                settings,
                secrets,
                clock,
                until=args.until,
                limit=args.limit,
                input_csv=args.input,
                allow_uncertain_manual=args.allow_uncertain_manual,
                retry=args.retry,
            )
    elif args.command == "discover":
        if args.until is None:
            raise ConfigError("Для discover нужен --until")
        async with jobs.manual_run_lock(secrets):
            await jobs.discover(settings, secrets, until=args.until, limit=args.limit)
    elif args.command == "name-candidates":
        if args.limit is None:
            raise ConfigError("Для name-candidates нужен --limit")
        async with jobs.manual_run_lock(secrets):
            await jobs.name_candidates(settings, secrets, clock, limit=args.limit)
    elif args.command == "quality-sample":
        output = args.output or Path("outputs/quality_sample.csv")
        await jobs.quality_sample(
            secrets,
            count=args.count,
            output=output,
            exclude=args.exclude,
            unclassified_only=args.unclassified_only,
        )
    elif args.command == "quality-evaluate":
        if args.input is None:
            raise ConfigError("Для quality-evaluate нужен --input CSV")
        await jobs.quality_evaluate(secrets, path=args.input)
    elif args.command == "quality-review":
        if args.input is None:
            raise ConfigError("Для quality-review нужен --input CSV")
        await jobs.quality_review(
            secrets,
            source=args.input,
            output=args.output or args.input.with_name(f"{args.input.stem}_review.xlsx"),
        )
    elif args.command == "quality-approve":
        if args.input is None or not args.reviewer or args.output is None:
            raise ConfigError("Для quality-approve нужны --input XLSX, --reviewer и --output DIR")
        await jobs.quality_approve(
            secrets,
            workbook=args.input,
            source=args.source,
            reviewer=args.reviewer,
            output_dir=args.output,
        )
    elif args.command == "quality-verify":
        if args.input is None:
            raise ConfigError("Для quality-verify нужен --input CSV контрольной серии")
        await jobs.quality_verify(
            settings,
            secrets,
            sample=args.input,
            output=args.output or args.input.with_name(args.input.stem + ".verification.json"),
        )
    elif args.command == "api":
        await jobs.serve_api(settings, secrets)
    elif args.command == "add-user":
        if not args.username or not args.role:
            raise ConfigError("Для add-user нужны --username и --role")
        REDACTED = getpass.getpass("Новый пароль: ")
        confirmation = getpass.getpass("Повторите пароль: ")
        if REDACTED != confirmation:
            raise ConfigError("Пароли не совпадают")
        try:
            await jobs.add_user(
                settings, secrets, username=args.username, REDACTED=REDACTED, role=args.role
            )
        except (ValueError, UserExistsError) as exc:
            raise ConfigError(str(exc) or "Пользователь уже существует") from exc


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        asyncio.run(_run(args))
    except (
        ConfigError,
        jobs.StaleCollectionError,
        jobs.IncompleteCollectionError,
        jobs.JobAlreadyRunningError,
    ) as exc:
        logger.error("{}", exc)
        return 1
    except WbAuthError as exc:
        logger.error("{}", exc)
        return 1
    except NotImplementedError as exc:
        logger.warning("Ещё не реализовано: {}", exc or args.command)
        return 2
    except KeyboardInterrupt:
        logger.info("Остановлено пользователем")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
