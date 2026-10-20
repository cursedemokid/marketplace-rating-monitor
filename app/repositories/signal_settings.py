"""Чтение и аудит изменения порогов из защищённого API."""

from __future__ import annotations

import json
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.signals import SignalRepository, SignalSetting


async def get_setting(session: AsyncSession, signal_type: str) -> SignalSetting | None:
    return (await SignalRepository(session).settings()).get(signal_type)


async def update_setting(
    session: AsyncSession,
    signal_type: str,
    changes: dict[str, object],
    *,
    changed_by: int,
) -> SignalSetting | None:
    """Проверенные поля обновляются вместе с записью аудита в одной транзакции."""
    previous = await session.execute(
        text("""
            select to_jsonb(s) as value
              from wb.signal_settings s
             where signal_type = :signal_type for update
        """),
        {"signal_type": signal_type},
    )
    before = previous.scalar_one_or_none()
    if before is None:
        return None
    params = {"signal_type": signal_type, **changes}
    for field in ("threshold", "alpha"):
        if params.get(field) is not None:
            params[field] = Decimal(str(params[field]))
    for field in (
        "threshold",
        "min_sample_size",
        "alpha",
        "suppress_hours",
        "baseline_days",
        "is_enabled",
    ):
        params.setdefault(field, None)
    await session.execute(
        text("""
            update wb.signal_settings
               set threshold = coalesce(:threshold, threshold),
                   min_sample_size = coalesce(:min_sample_size, min_sample_size),
                   alpha = coalesce(:alpha, alpha),
                   suppress_hours = coalesce(:suppress_hours, suppress_hours),
                   baseline_days = coalesce(:baseline_days, baseline_days),
                   is_enabled = coalesce(:is_enabled, is_enabled),
                   updated_at = now()
             where signal_type = :signal_type
        """),
        params,
    )
    after = await get_setting(session, signal_type)
    if after is None:
        raise RuntimeError("Настройка исчезла во время обновления")
    after_json = (
        await session.execute(
            text("select to_jsonb(s) from wb.signal_settings s where signal_type = :signal_type"),
            {"signal_type": signal_type},
        )
    ).scalar_one()
    await session.execute(
        text("""
            insert into wb.signal_settings_audit
                (signal_type, changed_by, before_value, after_value)
            values (:signal_type, :changed_by,
                    cast(:before_value as jsonb), cast(:after_value as jsonb))
        """),
        {
            "signal_type": signal_type,
            "changed_by": changed_by,
            "before_value": json.dumps(before, default=str, ensure_ascii=False),
            "after_value": json.dumps(after_json, default=str, ensure_ascii=False),
        },
    )
    return after
