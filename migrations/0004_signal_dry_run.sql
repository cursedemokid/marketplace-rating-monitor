-- Пробная доставка не является реальной отправкой. Отдельная отметка
-- позволяет сохранить sent_at честным и не выпускать пробную историю в чат
-- при переключении notify.dry_run на false.

alter table wb.signals
    add column if not exists dry_run_at timestamptz;

drop index if exists wb.signals_outbox_idx;
create index signals_outbox_idx on wb.signals (created_at, id)
    where sent_at is null and dry_run_at is null and not is_suppressed;
