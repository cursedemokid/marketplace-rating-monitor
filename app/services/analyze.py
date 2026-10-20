"""Прогон правил по снимкам: находит проблемные артикулы. TSK-204, TSK-205.

Порядок шагов: прочитать пороги → собрать приросты за день и за базовое
окно → прогнать четыре правила → проверить подавление → записать в историю.
Своей логики здесь нет, вся она в `rules.py` и `significance.py`.

Наружу на этом этапе ничего не уходит: отправкой занимается этап 3.
Сигнал пишется в базу всегда, даже подавленный, — чтобы в истории было
видно, что система изменение заметила (design § D5).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import Clock
from app.repositories.reasons import ReasonRepository
from app.repositories.signals import SignalRepository
from app.services import rules
from app.services.periods import Period, PeriodCounts, counts_for_period, rolling_baseline


@dataclass(frozen=True, slots=True)
class AnalyzeResult:
    products: int
    """Сколько артикулов удалось сравнить: есть снимки и за день, и за базу."""
    created: int
    suppressed: int
    skipped_no_data: int


async def run(
    session: AsyncSession,
    clock: Clock,
    *,
    run_date: dt.date,
) -> AnalyzeResult:
    """Разобрать день и завести сигналы."""
    repo = SignalRepository(session)
    reason_repo = ReasonRepository(session)
    settings = await repo.settings()
    enabled = {name: s for name, s in settings.items() if s.is_enabled}
    if not enabled:
        logger.warning("Все правила выключены в wb.signal_settings — сигналов не будет")
        return AnalyzeResult(products=0, created=0, suppressed=0, skipped_no_data=0)

    day = Period(start=run_date, end=run_date)
    today = await counts_for_period(session, day)
    if not today:
        logger.warning(
            "За {} нет пригодных снимков: нужен снимок и за этот день, и за предыдущий",
            run_date,
        )
        return AnalyzeResult(products=0, created=0, suppressed=0, skipped_no_data=0)

    # Настройки хранят baseline_days на каждое правило. Одинаковые окна
    # читаем один раз, но не подменяем короткое окно самым длинным.
    baselines: dict[int, dict[int, PeriodCounts]] = {}
    for days in {setting.baseline_days for setting in enabled.values()}:
        period = rolling_baseline(run_date, days)
        baselines[days] = await counts_for_period(session, period)
        logger.info("База анализа за {}: {} ({} дн.)", run_date, period, days)

    logger.info("Анализ за {}: артикулов со снимками {}", run_date, len(today))

    created = suppressed = skipped = 0
    now = clock.now()
    for nm_id, current in today.items():
        had_baseline = False
        for name, setting in enabled.items():
            base = baselines[setting.baseline_days].get(nm_id)
            if base is None:
                continue
            had_baseline = True
            rule = rules.ALL_RULES.get(name)
            if rule is None:
                logger.warning("В настройках есть тип {}, а правила под него нет", name)
                continue
            signal = rule(current, base, setting)
            if signal is None:
                continue
            if await repo.exists_for_period(nm_id, name, run_date):
                # Повторный прогон analyze за ту же дату не должен плодить
                # дубли в истории.
                continue
            if name == rules.NEGATIVE_SPIKE:
                summary = await reason_repo.top_reason(
                    nm_id=nm_id,
                    period_start=signal.period_start,
                    period_end=signal.period_end,
                )
                if summary is not None:
                    signal = replace(signal, ai_summary=summary)
            is_suppressed = await repo.is_suppressed(
                nm_id, name, now=now, suppress_hours=setting.suppress_hours
            )
            await repo.add(signal, created_at=now, is_suppressed=is_suppressed)
            if is_suppressed:
                suppressed += 1
            else:
                created += 1
            logger.info(
                "Сигнал {} по артикулу {}: {}{}",
                name,
                nm_id,
                signal.delta,
                " (подавлен, недавно уже был такой)" if is_suppressed else "",
            )
        if not had_baseline:
            skipped += 1

    # Транзакцию закрывает вызывающий: сервис не решает за него, когда
    # фиксировать. Это же делает прогон проверяемым — тест откатывает
    # сессию и база остаётся чистой.
    logger.info(
        "Анализ за {} завершён: сигналов {}, подавлено {}, без базы {}",
        run_date,
        created,
        suppressed,
        skipped,
    )
    return AnalyzeResult(
        products=len(today),
        created=created,
        suppressed=suppressed,
        skipped_no_data=skipped,
    )
