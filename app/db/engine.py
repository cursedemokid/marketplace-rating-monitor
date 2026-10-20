"""Движок SQLAlchemy.

design.md §2: бот и база на одном VPS, соединение локальное.
  1. Основной вариант — прямое подключение (`postgresql+asyncpg://`).
  2. Если соединение идёт через Supavisor в transaction mode (порт 6543),
     prepared statements не поддерживаются: нужны statement_cache_size=0
     и prepared_statement_cache_size=0, иначе поймаем плавающие ошибки
     на повторных запросах.
Пул: pool_size=5, max_overflow=5 — один чат, один процесс.

ВНИМАНИЕ (TSK-002): режим Supavisor определяется по порту 6543 — это
эвристика из design.md §2. На реальном VPS проверить фактический порт и
режим пулера, а не полагаться на неё.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

SUPAVISOR_TRANSACTION_PORT = 6543

POOL_SIZE = 5
MAX_OVERFLOW = 5


def uses_transaction_pooler(database_url: str) -> bool:
    return urlsplit(database_url).port == SUPAVISOR_TRANSACTION_PORT


def _with_query_param(url: str, key: str, value: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault(key, value)
    return urlunsplit(parts._replace(query=urlencode(query)))


def build_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    connect_args: dict[str, Any] = {}
    url = database_url

    if uses_transaction_pooler(url):
        logger.info(
            "Порт {} — считаем, что это Supavisor в transaction mode: "
            "prepared statements отключены",
            SUPAVISOR_TRANSACTION_PORT,
        )
        url = _with_query_param(url, "prepared_statement_cache_size", "0")
        connect_args["statement_cache_size"] = 0

    return create_async_engine(
        url,
        echo=echo,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_pre_ping=True,
        connect_args=connect_args,
    )


async def check_connection(engine: AsyncEngine) -> None:
    """Проверка живости на старте. Падаем сразу и понятно, а не на первом сообщении."""
    async with engine.connect() as conn:
        await conn.execute(text("select 1"))
