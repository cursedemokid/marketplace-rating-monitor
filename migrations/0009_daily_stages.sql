-- Долговечные отметки этапов дневной цепочки для восстановления после сбоя.
create table if not exists wb.daily_job_stages (
    run_date date primary key,
    collect_done boolean not null default false,
    analyze_done boolean not null default false,
    notify_done boolean not null default false,
    updated_at timestamptz not null default now()
);
