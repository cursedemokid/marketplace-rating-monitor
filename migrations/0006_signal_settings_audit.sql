-- Кто менял пороги и что было до/после: настройки влияют на сигналы.
create table if not exists wb.signal_settings_audit (
    id           bigserial primary key,
    signal_type  text        not null,
    changed_by   bigint      not null references wb.users (id),
    changed_at   timestamptz not null default now(),
    before_value jsonb       not null,
    after_value  jsonb       not null
);
create index if not exists signal_settings_audit_type_time_idx
    on wb.signal_settings_audit (signal_type, changed_at desc);
