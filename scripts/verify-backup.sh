#!/usr/bin/env bash
# Проверка архива восстановлением в отдельную временную базу.
set -euo pipefail

if [[ $# -ne 1 || ! -r $1 ]]; then
  printf 'Использование: %s ПУТЬ_К_АРХИВУ.dump\n' "$0" >&2
  exit 2
fi

archive=$1
cd "$(dirname "$0")/.."
check_db="wb_restore_check_$$"
created=false
cleanup() {
  if [[ $created == true ]]; then
    docker compose exec -T postgres dropdb -U wb --if-exists "$check_db" > /dev/null
  fi
}
trap cleanup EXIT

docker compose exec -T postgres createdb -U wb "$check_db"
created=true
docker compose exec -T postgres \
  pg_restore -U wb --no-owner --no-privileges -d "$check_db" < "$archive"
products=$(docker compose exec -T postgres \
  psql -U wb -d "$check_db" -tAc 'select count(*) from wb.products')
printf 'Восстановление проверено: товаров %s\n' "$products"
