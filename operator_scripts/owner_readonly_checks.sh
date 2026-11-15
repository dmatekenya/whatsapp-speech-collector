#!/usr/bin/env bash
# No modification to Docker, database, Caddyfile, SSH or gateway settings.
set -euo pipefail
printf '\n=== QC OWNER CONTRACT: READ-ONLY CHECKS ===\n'
sudo docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' --filter 'name=masakhane-supervision-gateway'
sudo docker exec -i whatsapp-speech-collector-postgres-1 sh -c 'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
SELECT version_num FROM public.alembic_version;
SELECT COALESCE(to_regclass('public.qc_human_reviews')::text,'NOT_INSTALLED') AS human_review_table;
SELECT rolname,rolcanlogin,rolsuper FROM pg_roles WHERE rolname IN ('masakhane_qc_writer','masakhane_supervision_ro') ORDER BY rolname;
SQL
if sudo ss -ltn | grep -q ':22222 '; then
 echo 'SSH 22222: port is listening (confirm whether it was intentionally opened)'
else
 echo 'SSH 22222: no listening TCP port reported'
fi
echo '--- configured SSH port(s) ---'
sudo sshd -T 2>/dev/null | grep '^port ' || true
if sudo test -f /opt/masakhane-supervision-gateway/hostinger-review-token-TRANSFER-AND-DELETE.txt; then
 echo 'TRANSFER TOKEN FILE: present — owner-approved deletion needed after verifying Hostinger storage'
else
 echo 'TRANSFER TOKEN FILE: absent'
fi
echo '--- check complete: no changes made ---'
