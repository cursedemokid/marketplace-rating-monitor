-- Разделение двух кабинетов WB без изменения глобального nm_id.
alter table wb.products
    add column account_key text not null default 'default';
create index products_account_monitored_idx
    on wb.products (account_key, nm_id) where is_monitored;

alter table wb.collection_runs
    add column account_key text not null default 'default';
create index collection_runs_account_date_idx
    on wb.collection_runs (account_key, run_date, status);
