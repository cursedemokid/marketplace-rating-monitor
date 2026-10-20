-- Дополнительные причины по проверке 70 отзывов заказчиком 15.10.2026.
-- «Не соответствует заявленному» — проверяемый факт (например, число штук),
-- «не оправдались ожидания» — субъективная оценка результата.
insert into wb.negative_reasons (name, created_by, is_active, is_approved) values
    ('не соответствует заявленному', 'seed', true, true),
    ('неудобная резинка', 'seed', true, true),
    ('холод после намокания', 'seed', true, true),
    ('большемерят', 'seed', true, true)
on conflict (name) do update
    set is_active = true,
        is_approved = true;
