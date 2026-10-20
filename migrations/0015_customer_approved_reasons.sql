-- Причины, подтверждённые заказчиком при разборе 100 реальных отзывов.
-- Исходные 16 причин из ТЗ сохраняются, справочник расширяется для всех товаров.
insert into wb.negative_reasons (name, created_by, is_active, is_approved) values
    ('ухудшение качества товара', 'seed', true, true),
    ('нет заявленного эффекта', 'seed', true, true),
    ('плохая стирка', 'seed', true, true),
    ('не оправдались ожидания', 'seed', true, true),
    ('мало пены', 'seed', true, true),
    ('аромат не соответствует заявленному', 'seed', true, true),
    ('слабый аромат', 'seed', true, true),
    ('высокая цена', 'seed', true, true),
    ('большой расход средства', 'seed', true, true),
    ('нечитаемый отзыв', 'seed', true, true)
on conflict (name) do update
    set is_active = true,
        is_approved = true;
