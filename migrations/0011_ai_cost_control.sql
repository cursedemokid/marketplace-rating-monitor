-- Project-wide paid AI request ledger and per-review claim.
-- The monthly limit is enforced under a transaction advisory lock by the repository.
create table if not exists wb.ai_cost_requests (
    id                  bigserial primary key,
    model               text not null,
    month_start         date not null,
    status              text not null check (status in ('reserved', 'settled', 'uncertain')),
    estimated_cost_usd  numeric(24, 12) not null check (estimated_cost_usd >= 0),
    actual_cost_usd     numeric(24, 12),
    input_tokens        bigint,
    output_tokens       bigint,
    reserved_at         timestamptz not null,
    settled_at          timestamptz,
    check (actual_cost_usd is null or actual_cost_usd >= 0),
    check (input_tokens is null or input_tokens >= 0),
    check (output_tokens is null or output_tokens >= 0)
);
create index if not exists ai_cost_requests_month_idx
    on wb.ai_cost_requests (month_start, status);

create table if not exists wb.ai_review_claims (
    feedback_id text primary key references wb.feedbacks (id),
    request_id bigint not null references wb.ai_cost_requests (id),
    claimed_at timestamptz not null
);
create index if not exists ai_review_claims_request_idx
    on wb.ai_review_claims (request_id);
