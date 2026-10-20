-- Локальные учётки и отзывные сеансы для одного внутреннего сервера.
create table if not exists wb.users (
    id             bigserial primary key,
    username       text        not null unique,
    REDACTED_hash  text        not null,
    role           text        not null check (role in ('viewer', 'admin')),
    is_active      boolean     not null default true,
    failed_logins  integer     not null default 0,
    locked_until   timestamptz,
    created_at     timestamptz not null default now(),
    check (username = lower(username))
);

create table if not exists wb.auth_sessions (
    token_hash   char(64)    primary key,
    user_id      bigint      not null references wb.users (id) on delete cascade,
    created_at   timestamptz not null,
    last_seen_at timestamptz not null,
    expires_at   timestamptz not null,
    revoked_at   timestamptz
);
create index if not exists auth_sessions_user_idx on wb.auth_sessions (user_id);
create index if not exists auth_sessions_expiry_idx on wb.auth_sessions (expires_at);
