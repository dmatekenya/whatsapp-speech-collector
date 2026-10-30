#!/usr/bin/env bash
# ===============
# DATABASE BACKUP
# ===============
# Dumps the database from the postgres container into ./backups (custom
# format, compressed), keeping the 14 most recent. Backups contain personal
# data (participants' names and numbers): they're readable by you only.
#
#   ./scripts/backup_db.sh
#
# Nightly at 02:30 (crontab -e on the droplet; adjust the path):
#   30 2 * * * /home/deploy/whatsapp-speech-collector/scripts/backup_db.sh >> /home/deploy/whatsapp-speech-collector/backups/backup.log 2>&1
#
# Restore one (stops the app first; replaces the current data):
#   docker compose stop api worker
#   docker compose exec -T postgres sh -c 'pg_restore --clean --if-exists --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < backups/<file>.dump
#   docker compose start api worker
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
mkdir -p backups

KEEP=14
stamp="$(date +%Y-%m-%d_%H%M%S)"
file="backups/speech_collector_${stamp}.dump"

# Credentials are read inside the container from its own environment.
docker compose exec -T postgres sh -c \
    'pg_dump --format=custom -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > "${file}.partial"
mv "${file}.partial" "${file}"
echo "$(date '+%F %T') backup written: ${file} ($(du -h "${file}" | cut -f1))"

# Keep only the newest $KEEP.
ls -1t backups/speech_collector_*.dump | tail -n +$((KEEP + 1)) | while read -r old; do
    rm -f -- "$old"
    echo "removed old backup: $old"
done
