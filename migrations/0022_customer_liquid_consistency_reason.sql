-- Подтверждено заказчиком 16.10.2026, строка 35 контрольной выборки из 120.
-- «Вода водой» о средстве для мытья посуды: жалоба на жидкую консистенцию.
insert into wb.negative_reasons (name, created_by, is_active, is_approved) values
    ('слишком жидкое средство', 'seed', true, true)
on conflict (name) do update
    set is_active = true,
        is_approved = true;
