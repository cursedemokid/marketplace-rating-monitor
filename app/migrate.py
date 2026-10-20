"""Раннер миграций: одна команда, forward-only, повторный запуск не падает.

Alembic не ставим (BRIEF-AI.md §2): база общая с Mini App, инструмент миграций
выбирается позже, а обычный нумерованный `.sql` переносится в любой.

    uv run python -m app.migrate            применить всё, чего нет в базе
    uv run python -m app.migrate --dry-run  только показать, что применилось бы

Правила:
  * файлы `migrations/NNNN_name.sql`, применяются по возрастанию номера;
  * каждый файл — одна транзакция: упал на середине, значит не применился вовсе;
  * применённые файлы фиксируются в `wb.schema_migrations` вместе
    с sha256; правка уже применённого файла — ошибка, а не тихий пропуск:
    forward-only означает новый файл, а не редактирование старого;
  * advisory lock не даёт двум процессам мигрировать одновременно.

Работаем на голом asyncpg, а не через SQLAlchemy: файл миграции содержит
несколько команд, а SQLAlchemy гоняет запросы через prepared statement, куда
несколько команд не помещаются. Простой протокол asyncpg это умеет.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg
from loguru import logger

from app.config import ConfigError, load_secrets
from app.db.engine import uses_transaction_pooler
from app.logging import setup_logging

MIGRATIONS_DIR = Path("migrations")

FILENAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")

# Произвольная, но постоянная константа: второй процесс с тем же ключом будет
# ждать, а не применять миграции параллельно.
ADVISORY_LOCK_KEY = 8_314_002_271_553_001

BOOTSTRAP = """
create schema if not exists wb;

create table if not exists wb.schema_migrations (
    version    integer     primary key,
    name       text        not null,
    checksum   text        not null,
    applied_at timestamptz not null default now()
);
"""

# Параметры, которые понимает диалект SQLAlchemy, но не понимает сам asyncpg.
SQLALCHEMY_ONLY_PARAMS = frozenset({"prepared_statement_cache_size"})

EXIT_BAD_CONFIG = 2
EXIT_MIGRATION_FAILED = 5


class MigrationError(Exception):
    """Миграции применить нельзя. Текст рассчитан на человека."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str
    checksum: str

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


def to_asyncpg_dsn(database_url: str) -> str:
    """`postgresql+asyncpg://...` -> DSN, который примет сам asyncpg."""
    parts = urlsplit(database_url)
    scheme = parts.scheme.split("+", 1)[0]
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key not in SQLALCHEMY_ONLY_PARAMS
    ]
    return urlunsplit(parts._replace(scheme=scheme, query=urlencode(query)))


def discover(directory: Path) -> list[Migration]:
    """Собрать миграции из каталога, по возрастанию номера."""
    if not directory.is_dir():
        msg = f"{directory}: каталога с миграциями нет"
        raise MigrationError(msg)

    found: dict[int, Migration] = {}
    for path in sorted(directory.glob("*.sql")):
        match = FILENAME.match(path.name)
        if match is None:
            # Иначе файл с опечаткой в имени молча не применится.
            msg = f"{path.name}: имя не по формату NNNN_имя_латиницей.sql"
            raise MigrationError(msg)

        version = int(match["version"])
        sql = path.read_text(encoding="utf-8")
        migration = Migration(
            version=version,
            name=match["name"],
            path=path,
            sql=sql,
            checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        )

        if version in found:
            msg = f"номер {version:04d} занят дважды: {found[version].path.name} и {path.name}"
            raise MigrationError(msg)
        found[version] = migration

    return [found[version] for version in sorted(found)]


async def _applied(conn: asyncpg.Connection) -> dict[int, tuple[str, str]]:
    """Что уже применено. Пусто, если таблицы учёта ещё нет (режим --dry-run)."""
    exists = await conn.fetchval("select to_regclass('wb.schema_migrations') is not null")
    if not exists:
        return {}
    rows = await conn.fetch("select version, name, checksum from wb.schema_migrations")
    return {int(row["version"]): (str(row["name"]), str(row["checksum"])) for row in rows}


def _check_already_applied(migration: Migration, name: str, checksum: str) -> None:
    if checksum == migration.checksum:
        return
    msg = (
        f"{migration.path.name}: файл изменился после применения "
        f"(в базе {name}, sha256 {checksum[:12]}…, на диске {migration.checksum[:12]}…).\n"
        "Миграции forward-only: чтобы что-то поправить, добавь новый файл, "
        "а этот верни в прежний вид."
    )
    raise MigrationError(msg)


async def run_migrations(
    conn: asyncpg.Connection,
    migrations: list[Migration],
    *,
    dry_run: bool = False,
) -> list[Migration]:
    """Применить недостающие миграции. Возвращает те, что применились.

    В режиме dry_run база не меняется вообще: ни схемы, ни таблицы учёта,
    ни блокировки. Иначе «посмотреть, что будет» само меняло бы состояние.
    """
    if not dry_run:
        await conn.execute(BOOTSTRAP)
        await conn.execute("select pg_advisory_lock($1)", ADVISORY_LOCK_KEY)
    try:
        applied = await _applied(conn)

        orphans = sorted(set(applied) - {m.version for m in migrations})
        if orphans:
            logger.warning(
                "В базе отмечены миграции, которых нет на диске: {}",
                ", ".join(f"{v:04d}" for v in orphans),
            )

        done: list[Migration] = []
        for migration in migrations:
            if migration.version in applied:
                name, checksum = applied[migration.version]
                _check_already_applied(migration, name, checksum)
                logger.debug("{} уже применена", migration.label)
                continue

            if dry_run:
                logger.info("{} — будет применена", migration.label)
                done.append(migration)
                continue

            async with conn.transaction():
                await conn.execute(migration.sql)
                await conn.execute(
                    "insert into wb.schema_migrations (version, name, checksum) "
                    "values ($1, $2, $3)",
                    migration.version,
                    migration.name,
                    migration.checksum,
                )
            logger.info("{} применена", migration.label)
            done.append(migration)

        return done
    finally:
        if not dry_run:
            await conn.execute("select pg_advisory_unlock($1)", ADVISORY_LOCK_KEY)


async def connect(database_url: str) -> asyncpg.Connection:
    dsn = to_asyncpg_dsn(database_url)
    # Через Supavisor в transaction mode prepared statements не работают.
    cache_size = 0 if uses_transaction_pooler(database_url) else 100
    return await asyncpg.connect(dsn, statement_cache_size=cache_size)


async def migrate(database_url: str, directory: Path, *, dry_run: bool = False) -> list[Migration]:
    migrations = discover(directory)
    if not migrations:
        logger.warning("{}: миграций не найдено", directory)
        return []

    conn = await connect(database_url)
    try:
        return await run_migrations(conn, migrations, dry_run=dry_run)
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m app.migrate",
        description="Применить SQL-миграции схемы wb (forward-only).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="показать, что применилось бы, ничего не меняя",
    )
    parser.add_argument(
        "--migrations",
        type=Path,
        default=MIGRATIONS_DIR,
        help=f"каталог с файлами NNNN_name.sql (по умолчанию {MIGRATIONS_DIR})",
    )
    args = parser.parse_args(argv)

    setup_logging()

    try:
        secrets = load_secrets()
    except ConfigError as exc:
        sys.stderr.write(f"\n{exc}\n\n")
        raise SystemExit(EXIT_BAD_CONFIG) from None

    try:
        done = asyncio.run(migrate(secrets.database_url, args.migrations, dry_run=args.dry_run))
    except MigrationError as exc:
        logger.error("Миграции не применены: {}", exc)
        raise SystemExit(EXIT_MIGRATION_FAILED) from None
    except (asyncpg.PostgresError, OSError) as exc:
        logger.error("Миграции не применены: {}", exc)
        raise SystemExit(EXIT_MIGRATION_FAILED) from None

    if not done:
        logger.info("Всё уже применено, база в актуальном состоянии")
    elif args.dry_run:
        logger.info("Применилось бы миграций: {}", len(done))
    else:
        logger.info("Применено миграций: {}", len(done))


if __name__ == "__main__":
    main()
