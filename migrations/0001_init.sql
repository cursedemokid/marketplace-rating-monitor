-- Начальная схема. Источник — .kiro/specs/wb-rating-monitor/design.md § D3.
--
-- Принцип: храним только факты (снимки, отзывы, сигналы). Всё производное —
-- дельты, доли, проценты — считаем на лету (FR-2.2), чтобы правка формулы
-- не требовала миграции истории.

create schema if not exists wb;

-- Товары: связь артикула продавца и кода Wildberries (G-04)
create table if not exists wb.products (
    nm_id        bigint primary key,          -- код Wildberries
    vendor_code  text        not null,        -- артикул продавца, DemoPhone
    brand        text,
    subject_name text,                        -- предмет Wildberries
    size         text,
    is_monitored boolean     not null default true,
    updated_at   timestamptz not null
);
create index if not exists products_vendor_code_idx on wb.products (vendor_code);

-- Снимок показателей на дату. Ядро всей системы.
-- Первичный ключ (nm_id, snapshot_date) — защита от дублей, FR-1.3.
create table if not exists wb.daily_snapshots (
    nm_id          bigint  not null references wb.products (nm_id),
    snapshot_date  date    not null,          -- дата по Europe/Moscow
    rating         numeric(3, 2),
    ratings_total  integer not null,
    ratings_5      integer not null,
    ratings_4      integer not null,
    ratings_3      integer not null,
    ratings_2      integer not null,
    ratings_1      integer not null,
    buyout_percent numeric(5, 2),
    orders_count   integer,
    buyouts_count  integer,
    collected_at   timestamptz not null,
    primary key (nm_id, snapshot_date)
);

-- Отзывы. Первичный ключ — id от Wildberries: повторный прогон не плодит дубли.
create table if not exists wb.feedbacks (
    id           text     primary key,
    nm_id        bigint   not null references wb.products (nm_id),
    valuation    smallint not null check (valuation between 1 and 5),
    text         text,
    pros         text,
    cons         text,
    created_at   timestamptz not null,
    created_date date        not null,        -- по Europe/Moscow, для группировки
    imported_at  timestamptz not null
);
create index if not exists feedbacks_nm_date_idx on wb.feedbacks (nm_id, created_date);
create index if not exists feedbacks_negative_idx on wb.feedbacks (created_date)
    where valuation <= 3;

-- Справочник причин негатива. is_approved = false — ждёт модерации (FR-5.5).
create table if not exists wb.negative_reasons (
    id          serial  primary key,
    name        text    not null unique,
    is_active   boolean not null default true,
    is_approved boolean not null default true,
    created_by  text    not null,             -- 'seed' | 'ai' | 'user:<login>'
    created_at  timestamptz not null default now()
);

-- Связь отзыв ↔ причина. Один отзыв может иметь несколько причин (G-05).
create table if not exists wb.feedback_reasons (
    feedback_id   text    not null references wb.feedbacks (id),
    reason_id     integer not null references wb.negative_reasons (id),
    confidence    numeric(3, 2),
    model         text    not null,
    classified_at timestamptz not null,
    primary key (feedback_id, reason_id)
);

-- История сигналов, FR-3.7. Подавленный сигнал тоже пишется, с sent_at = null.
create table if not exists wb.signals (
    id           bigserial primary key,
    nm_id        bigint    not null references wb.products (nm_id),
    signal_type  text      not null,          -- rating_change | negative_spike | ...
    period_start date      not null,
    period_end   date      not null,
    value_before numeric,
    value_after  numeric,
    delta        numeric,
    ai_summary   text,
    payload      jsonb,                       -- детали расчёта, для разбора спорных
    created_at   timestamptz not null,
    sent_at      timestamptz,
    send_error   text
);
create index if not exists signals_lookup_idx
    on wb.signals (nm_id, signal_type, created_at desc);

-- Журнал прогонов сбора
create table if not exists wb.collection_runs (
    id              bigserial primary key,
    run_date        date        not null,
    started_at      timestamptz not null,
    finished_at     timestamptz,
    status          text        not null,     -- running | ok | partial | failed
    products_ok     integer     not null default 0,
    products_failed integer     not null default 0,
    error           text
);

-- Товарная номенклатура заказчика, G-03. Восемь фильтров, которых нет в WB.
create table if not exists wb.nomenclature (
    vendor_code text primary key,
    pack        text,     -- Пачка
    line        text,     -- Линейка
    type        text,     -- Тип
    capacity    text,     -- Вложимость
    category    text,     -- Категория
    brand2      text,     -- Бренд-2
    kit         text,     -- Комплект
    kit_qty     integer,  -- Количество в комплекте
    imported_at timestamptz not null
);

-- Справочник причин из ТЗ п. 6. Расширяется только через модерацию (FR-5.5).
insert into wb.negative_reasons (name, created_by) values
    ('протекают', 'seed'),
    ('пустой отзыв', 'seed'),
    ('доставка / ошибка', 'seed'),
    ('маломерят', 'seed'),
    ('впитываемость', 'seed'),
    ('аллергия / раздражение', 'seed'),
    ('комкуются', 'seed'),
    ('причина не указана', 'seed'),
    ('жёсткие', 'seed'),
    ('неприятный запах', 'seed'),
    ('брак', 'seed'),
    ('запах химии', 'seed'),
    ('тонкие', 'seed'),
    ('размер', 'seed'),
    ('баллы за отзыв', 'seed'),
    ('толстые', 'seed')
on conflict (name) do nothing;
