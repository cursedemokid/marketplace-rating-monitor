-- Preserve unresolved provider costs and audit explicit manual admissions made
-- under the full estimated charge. No automatic job writes this table.
create table if not exists wb.ai_uncertain_admissions (
    request_id           bigint not null references wb.ai_cost_requests(id),
    uncertain_request_id bigint not null references wb.ai_cost_requests(id),
    admitted_at          timestamptz not null,
    primary key (request_id, uncertain_request_id),
    check (request_id <> uncertain_request_id)
);
