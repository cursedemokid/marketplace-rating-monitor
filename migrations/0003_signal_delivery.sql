-- Доставка сигналов. Этап 3.
--
-- Пробел, вскрывшийся при реализации: подавленный сигнал пишется в историю
-- с sent_at = null — ровно как и обычный неотправленный. Отправщик их не
-- различал бы и слал подавленное в чат, то есть антиспам не работал бы
-- вовсе. Нужен явный признак.

alter table wb.signals
    add column if not exists is_suppressed boolean not null default false;

comment on column wb.signals.is_suppressed is
    'Сигнал распознан, но наружу не идёт: такой же был недавно (FR-3.6)';

-- Очередь отправки: берём только неотправленные и неподавленные.
create index if not exists signals_outbox_idx
    on wb.signals (created_at)
    where sent_at is null and not is_suppressed;
