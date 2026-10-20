-- One paid naming attempt per discovery candidate. The linked cost request is
-- also the durable claim when a provider response or its usage is uncertain.
create table if not exists wb.ai_naming_requests (
    candidate_id bigint primary key references wb.discovery_candidates(id),
    request_id  bigint not null unique references wb.ai_cost_requests(id),
    created_at  timestamptz not null
);
