"""Сигналы и их пороги. FR-3.6, FR-3.7, FR-3.8, TSK-205.

Пороги и история лежат в одном репозитории намеренно: таблица настроек —
это один SELECT на четыре строки, отдельный файл под неё был бы пустым
слоем.

Подавление повторов считается запросом по самой истории сигналов, а не
ключом в Redis (отклонение от design § D5). Так решение переживает
перезапуск и остаётся аудируемым: видно, какой именно прошлый сигнал
закрыл дорогу новому.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.significance import SignificanceConfig


@dataclass(frozen=True, slots=True)
class SignalSetting:
    """Строка `wb.signal_settings`. Смысл `threshold` зависит от типа сигнала."""

    signal_type: str
    threshold: float
    min_sample_size: int
    alpha: float
    suppress_hours: int
    baseline_days: int
    is_enabled: bool

    def significance(self) -> SignificanceConfig:
        return SignificanceConfig(
            min_delta_pp=self.threshold,
            min_sample_size=self.min_sample_size,
            alpha=self.alpha,
        )


def _numeric(value: float | None) -> Decimal | None:
    """Float → Decimal перед записью в numeric.

    Без этого драйвер отправляет число как float8, и в базе оседает
    двоичный хвост: 4,93 превращается в 4,5139406806.
    В отчёт заказчику такое попадать не должно.
    """
    return None if value is None else Decimal(str(value))


@dataclass(frozen=True, slots=True)
class PendingSignal:
    """Сигнал, готовый к отправке. Артикул продавца — для текста сообщения."""

    id: int
    nm_id: int
    vendor_code: str
    signal_type: str
    period_start: dt.date
    period_end: dt.date
    value_before: float | None
    value_after: float | None
    delta: float | None
    ai_summary: str | None
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SignalRow:
    nm_id: int
    signal_type: str
    period_start: dt.date
    period_end: dt.date
    value_before: float | None
    value_after: float | None
    delta: float | None
    payload: dict[str, Any]
    ai_summary: str | None = None


_SETTINGS = text("""
    select signal_type, threshold, min_sample_size, alpha,
           suppress_hours, baseline_days, is_enabled
      from wb.signal_settings
""")

_INSERT = text("""
    insert into wb.signals (
        nm_id, signal_type, period_start, period_end,
        value_before, value_after, delta, ai_summary, payload, is_suppressed, created_at
    ) values (
        :nm_id, :signal_type, :period_start, :period_end,
        :value_before, :value_after, :delta, :ai_summary, cast(:payload as jsonb),
        :is_suppressed, :created_at
    )
    returning id
""")

_SUPPRESSED = text("""
    select exists (
        select 1
          from wb.signals
         where nm_id = :nm_id
           and signal_type = :signal_type
           and not is_suppressed
           and dry_run_at is null
           and created_at > :since
    )
""")

_EXISTS_FOR_PERIOD = text("""
    select exists (
        select 1
          from wb.signals
         where nm_id = :nm_id
           and signal_type = :signal_type
           and period_end = :period_end
    )
""")


class SignalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def settings(self) -> dict[str, SignalSetting]:
        """Пороги из базы. Не кэшируем: правка применяется со следующего прогона."""
        result = await self._session.execute(_SETTINGS)
        return {
            str(row.signal_type): SignalSetting(
                signal_type=str(row.signal_type),
                threshold=float(row.threshold),
                min_sample_size=int(row.min_sample_size),
                alpha=float(row.alpha),
                suppress_hours=int(row.suppress_hours),
                baseline_days=int(row.baseline_days),
                is_enabled=bool(row.is_enabled),
            )
            for row in result.all()
        }

    async def is_suppressed(
        self,
        nm_id: int,
        signal_type: str,
        *,
        now: dt.datetime,
        suppress_hours: int,
    ) -> bool:
        """Был ли такой же сигнал по этому артикулу в окне подавления. FR-3.6."""
        result = await self._session.execute(
            _SUPPRESSED,
            {
                "nm_id": nm_id,
                "signal_type": signal_type,
                "since": now - dt.timedelta(hours=suppress_hours),
            },
        )
        return bool(result.scalar_one())

    async def exists_for_period(self, nm_id: int, signal_type: str, period_end: dt.date) -> bool:
        """Защита от повторного прогона analyze за ту же дату.

        Уникального ключа на `signals` нет и быть не должно: история копится,
        и один и тот же тип сигнала по артикулу законно повторяется в разные
        дни. Поэтому идемпотентность держится проверкой, а не ограничением.
        """
        result = await self._session.execute(
            _EXISTS_FOR_PERIOD,
            {"nm_id": nm_id, "signal_type": signal_type, "period_end": period_end},
        )
        return bool(result.scalar_one())

    async def add(
        self,
        row: SignalRow,
        *,
        created_at: dt.datetime,
        is_suppressed: bool = False,
    ) -> int:
        result = await self._session.execute(
            _INSERT,
            {
                "nm_id": row.nm_id,
                "signal_type": row.signal_type,
                "period_start": row.period_start,
                "period_end": row.period_end,
                "value_before": _numeric(row.value_before),
                "value_after": _numeric(row.value_after),
                "delta": _numeric(row.delta),
                "ai_summary": row.ai_summary,
                "payload": json.dumps(row.payload, ensure_ascii=False),
                "is_suppressed": is_suppressed,
                "created_at": created_at,
            },
        )
        return int(result.scalar_one())

    async def count_for_period(self, period_end: dt.date) -> int:
        result = await self._session.execute(
            text("select count(*) from wb.signals where period_end = :d"),
            {"d": period_end},
        )
        return int(result.scalar_one())

    async def refresh_unsent_summaries(self) -> None:
        """Use the latest classified reasons for signals still awaiting delivery."""
        await self._session.execute(
            text("""
                update wb.signals s
                   set ai_summary = (
                       select r.name
                         from wb.feedbacks f
                         join wb.feedback_reasons fr on fr.feedback_id = f.id
                         join wb.negative_reasons r on r.id = fr.reason_id
                        where f.nm_id = s.nm_id
                          and f.valuation <= 3
                          and f.created_date between s.period_start and s.period_end
                          and r.is_active and r.is_approved
                          and not r.is_external
                          and r.name <> 'причина не указана'
                        group by r.id, r.name
                        order by count(*) desc, r.name
                        limit 1
                   )
                 where s.signal_type = 'negative_spike'
                   and s.sent_at is null and s.dry_run_at is null
                   and not s.is_suppressed
            """)
        )

    async def outbox(self, limit: int = 100, *, after_id: int = 0) -> list[PendingSignal]:
        """Очередь отправки: не ушло и не подавлено. FR-4.3.

        Подавленные отсеиваются здесь, а не в отправщике: сигнал в истории
        остаётся (система его заметила), но в чат не идёт.
        """
        result = await self._session.execute(
            text("""
                select s.id, s.nm_id, p.vendor_code, s.signal_type,
                       s.period_start, s.period_end,
                       s.value_before, s.value_after, s.delta, s.ai_summary, s.payload
                  from wb.signals s
                  join wb.products p on p.nm_id = s.nm_id
                 where s.sent_at is null
                   and s.dry_run_at is null
                   and not s.is_suppressed
                   and s.id > :after_id
                 order by s.id
                 limit :limit
                 for update of s skip locked
            """),
            {"limit": limit, "after_id": after_id},
        )
        return [
            PendingSignal(
                id=int(row.id),
                nm_id=int(row.nm_id),
                vendor_code=str(row.vendor_code),
                signal_type=str(row.signal_type),
                period_start=row.period_start,
                period_end=row.period_end,
                value_before=float(row.value_before) if row.value_before is not None else None,
                value_after=float(row.value_after) if row.value_after is not None else None,
                delta=float(row.delta) if row.delta is not None else None,
                ai_summary=row.ai_summary,
                payload=row.payload or {},
            )
            for row in result.all()
        ]

    async def mark_sent(self, signal_id: int, *, sent_at: dt.datetime, message_id: int) -> None:
        await self._session.execute(
            text("""
                update wb.signals
                   set sent_at = :sent_at,
                       send_error = null,
                       payload = payload || cast(:extra as jsonb)
                 where id = :id
            """),
            {
                "id": signal_id,
                "sent_at": sent_at,
                "extra": json.dumps({"message_id": message_id}),
            },
        )

    async def mark_dry_run(self, signal_id: int, *, observed_at: dt.datetime) -> None:
        """Отметить пробный сигнал без ложной отметки о реальной отправке."""
        await self._session.execute(
            text("update wb.signals set dry_run_at = :observed_at where id = :id"),
            {"id": signal_id, "observed_at": observed_at},
        )

    async def mark_failed(self, signal_id: int, error: str) -> None:
        """Сигнал остаётся неотправленным и попадёт в следующую попытку. FR-4.3."""
        await self._session.execute(
            text("update wb.signals set send_error = :error where id = :id"),
            {"id": signal_id, "error": error[:500]},
        )
