-- Distinguish a bounded unknown provider charge from an over-bound response.
alter table wb.ai_cost_requests
    add column if not exists consumes_monthly_cap boolean not null default false;

-- Older over-bound calls used a $100 estimate as the blocking sentinel.
update wb.ai_cost_requests
   set consumes_monthly_cap = true
 where status = 'uncertain' and estimated_cost_usd >= 100;

-- Keep each paid retry linked to its original request without replacing the
-- original claim. The primary key permits at most one retry per review.
create table if not exists wb.ai_review_retries (
    feedback_id        text primary key references wb.feedbacks (id),
    previous_request_id bigint not null references wb.ai_cost_requests (id),
    retry_request_id    bigint not null references wb.ai_cost_requests (id),
    attempted_at        timestamptz not null,
    check (previous_request_id <> retry_request_id)
);
create index if not exists ai_review_retries_retry_request_idx
    on wb.ai_review_retries (retry_request_id);
