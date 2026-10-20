-- Keep the AI outcome separate from the durable completion flags of other stages.
alter table wb.daily_job_stages
    add column classify_status text not null default 'pending',
    add column classify_detail text,
    add constraint daily_job_classify_status_check
        check (classify_status in (
            'pending', 'complete', 'partial', 'disabled',
            'deferred_budget', 'deferred_provider', 'deferred_config', 'deferred_limit'
        ));

update wb.daily_job_stages
   set classify_status = case when classify_done then 'complete' else 'pending' end;
