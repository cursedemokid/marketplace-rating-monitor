-- Название карточки WB (`title`) для фильтра «Номенклатура».
alter table wb.products add column if not exists title text;
