alter table wb.negative_reasons
    add column is_external boolean not null default false;

insert into wb.negative_reasons (name, created_by, is_active, is_approved, is_external)
values ('работа ПВЗ', 'seed', true, true, true)
on conflict (name) do update
    set is_active = true,
        is_approved = true,
        is_external = true;
