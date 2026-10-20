-- Кандидаты причин, ожидающие явного решения администратора.
create table if not exists wb.discovery_candidates (
    id            bigserial primary key,
    proposed_name text,
    status        text not null default 'pending'
                  check (status in ('pending', 'approved', 'rejected', 'merged')),
    merged_into   bigint references wb.discovery_candidates(id),
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    resolved_at   timestamptz,
    check ((status = 'merged') = (merged_into is not null))
);
create index if not exists discovery_candidates_status_idx
    on wb.discovery_candidates (status, id);

create table if not exists wb.discovery_candidate_reviews (
    candidate_id bigint not null references wb.discovery_candidates(id) on delete cascade,
    feedback_id  text not null unique references wb.feedbacks(id) on delete cascade,
    primary key (candidate_id, feedback_id)
);

-- Отзывы без найденного кластера тоже помечаются, чтобы последующие прогоны
-- продвигались по истории, а не бесконечно анализировали шумовую выборку.
create table if not exists wb.discovery_examined_reviews (
    feedback_id text primary key references wb.feedbacks(id) on delete cascade,
    examined_at timestamptz not null default now()
);
