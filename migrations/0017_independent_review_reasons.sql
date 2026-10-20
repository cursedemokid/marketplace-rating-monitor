-- Причины, подтверждённые заказчиком при проверке независимых 100 отзывов.
-- Похвала при оценке 1–3 звезды относится к ошибочной оценке, если жалобы нет.
-- Явный физический дискомфорт, включая «парниковый эффект», выделяется отдельно.
insert into wb.negative_reasons (name, created_by, is_active, is_approved) values
    ('ошибочно низкая оценка', 'seed', true, true),
    ('дискомфорт', 'seed', true, true)
on conflict (name) do update
    set is_active = true,
        is_approved = true;
