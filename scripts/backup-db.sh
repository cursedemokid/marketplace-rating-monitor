#!/usr/bin/env bash
# Резервная копия локальной PostgreSQL из docker-compose.yml.
set -euo pipefail

cd "$(dirname "$0")/.."
umask 077

backup_dir=${WB_BACKUP_DIR:-"$PWD/backups"}
mkdir -p -- "$backup_dir"
chmod 700 -- "$backup_dir"

temporary=$(mktemp "$backup_dir/.wb_rating_monitor.XXXXXXXX.dump")
trap 'rm -f -- "$temporary"' EXIT

docker compose exec -T postgres \
  pg_dump -U wb -d wb_rating_monitor --format=custom > "$temporary"
docker compose exec -T postgres pg_restore --list < "$temporary" > /dev/null

archive="$backup_dir/wb_rating_monitor_$(date -u +%Y%m%dT%H%M%SZ)_$$.dump"
mv -- "$temporary" "$archive"
printf 'Резервная копия: %s\n' "$archive"
