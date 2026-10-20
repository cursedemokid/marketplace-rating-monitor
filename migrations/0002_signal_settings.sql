-- Пороги сигналов в базе, а не в конфиге. FR-3.8, AC-3:
-- «изменение порога в интерфейсе меняет поведение без перезапуска».
--
-- До появления экрана настроек (этап 6) правятся SQL-запросом. Читаются
-- на каждый прогон analyze и не кэшируются, поэтому правка применяется
-- со следующего прогона.

create table if not exists wb.signal_settings (
    signal_type     text primary key,
    threshold       numeric not null,
    -- Смысл порога зависит от типа сигнала:
    --   rating_change  — пункты рейтинга (0,01)
    --   negative_spike — процентные пункты доли оценок 1-3
    --   top_rating_drop— процентные пункты доли оценок 5
    --   buyout_drop    — процентные пункты выкупа (0,2 из ТЗ п. 8)
    min_sample_size integer     not null,
    alpha           numeric     not null default 0.01,
    -- Уровень значимости z-теста. Для правил без теста не используется.
    suppress_hours  integer     not null default 24,
    baseline_days   integer     not null default 14,
    -- Длина скользящего окна, с которым сравнивается текущий день.
    -- Дневное сравнение при 10-20 оценках в сутки слишком шумное.
    is_enabled      boolean     not null default true,
    updated_at      timestamptz not null default now()
);

-- Стартовые значения из design.md § D5 и docs/02 → G-06. Предложены нами,
-- заказчик их ещё не подтвердил (вопрос Q6), калибруются на первых двух
-- неделях работы в режиме notify.dry_run.
insert into wb.signal_settings
    (signal_type, threshold, min_sample_size, alpha, suppress_hours, baseline_days)
values
    ('rating_change',   0.01, 0,  0.01, 24, 14),
    ('negative_spike',  5.0,  10, 0.01, 24, 14),
    ('top_rating_drop', 5.0,  10, 0.01, 24, 14),
    ('buyout_drop',     0.2,  30, 0.01, 24, 14)
on conflict (signal_type) do nothing;
