-- Номенклатура привязана к конкретному кабинету и артикулу продавца.
-- Старые записи сохраняют ключ default.
alter table wb.nomenclature
    add column account_key text not null default 'default';

alter table wb.nomenclature
    drop constraint nomenclature_pkey;

alter table wb.nomenclature
    add primary key (account_key, vendor_code);

alter table wb.nomenclature
    add constraint nomenclature_kit_qty_nonnegative
    check (kit_qty is null or kit_qty >= 0);
