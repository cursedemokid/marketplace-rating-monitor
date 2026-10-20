"""Настройка логов. Один вызов на старте процесса."""

from __future__ import annotations

import sys

from loguru import logger


def setup_logging(*, level: str = "INFO", sentry_dsn: str = "") -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>"
        ),
    )
    if sentry_dsn:
        import sentry_sdk

        sentry_sdk.init(dsn=sentry_dsn, traces_sample_rate=0.0)
        logger.info("Sentry подключён")
