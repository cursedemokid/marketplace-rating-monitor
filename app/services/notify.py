"""Доставка сигналов в чат. FR-4.1, FR-4.3, FR-4.4, TSK-301…TSK-305.

Прогон берёт очередь неотправленных сигналов, рендерит текст и отдаёт
каналу. Своей логики о том, что считать проблемой, здесь нет — она вся
в `rules.py`. Здесь только доставка и её последствия.

Недоставленный сигнал не теряется: `sent_at` остаётся пустым, причина
пишется в `send_error`, и следующий прогон попробует снова (FR-4.3).
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.messenger.base import NotificationChannel
from app.clock import Clock
from app.repositories.signals import SignalRepository
from app.services.messages import render


@dataclass(frozen=True, slots=True)
class NotifyResult:
    sent: int
    failed: int
    simulated: int = 0


async def run(
    session: AsyncSession,
    clock: Clock,
    channel: NotificationChannel | None,
    *,
    limit: int = 100,
    dry_run: bool = False,
) -> NotifyResult:
    """Отправить всё, что накопилось в очереди."""
    if channel is None and not dry_run:
        msg = "Для реальной доставки нужен канал уведомлений"
        raise ValueError(msg)
    repo = SignalRepository(session)
    await repo.refresh_unsent_summaries()
    sent = failed = simulated = 0
    after_id = 0
    while pending := await repo.outbox(limit=limit, after_id=after_id):
        logger.info(
            "К обработке сигналов: {}, канал {}",
            len(pending),
            "dry_run" if dry_run else channel.name if channel is not None else "unknown",
        )
        for signal in pending:
            after_id = signal.id
            if dry_run:
                logger.info("[dry_run] сигнал не отправлен, текст:\n{}", render(signal))
                await repo.mark_dry_run(signal.id, observed_at=clock.now())
                simulated += 1
                continue
            assert channel is not None
            result = await channel.send(render(signal), payload_id=f"signal-{signal.id}")
            if result.ok:
                await repo.mark_sent(
                    signal.id,
                    sent_at=clock.now(),
                    message_id=result.message_id or 0,
                )
                sent += 1
            else:
                await repo.mark_failed(signal.id, result.error or "неизвестная ошибка")
                failed += 1

    if not (sent or failed or simulated):
        logger.info("Очередь отправки пуста")
    logger.info("Отправлено {}, не удалось {}, пробных {}", sent, failed, simulated)
    return NotifyResult(sent=sent, failed=failed, simulated=simulated)
