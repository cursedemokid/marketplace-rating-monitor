-- Классификация становится отдельным восстанавливаемым этапом перед анализом.
alter table wb.daily_job_stages
    add column classify_done boolean not null default false;

-- Завершённые до добавления этапа даты уже были обработаны без автоматического AI.
update wb.daily_job_stages
   set classify_done = true
 where collect_done;
