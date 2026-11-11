#!/usr/bin/env bash
# NO production mutations. Run as an authorized Docker operator on DigitalOcean.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo '=== Masakhane Tier 3 — READ-ONLY preflight ==='
python3 -m py_compile "$ROOT/gateway/gateway.py" "$ROOT/gateway/writeback.py"
python3 -m py_compile "$ROOT/collector/migrations/versions/9b4d0e72c1f0_supervisor_human_reviews.py"
echo 'Python syntax: OK'
echo 'Collector container status:'
sudo docker ps --filter 'name=whatsapp-speech-collector' --format 'table {{.Names}}\t{{.Status}}'
echo 'Gateway container status:'
sudo docker ps --filter 'name=masakhane-supervision-gateway' --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
echo 'Existing collector revisions:'
ls -l "$ROOT/collector/migrations/versions/9b4d0e72c1f0_supervisor_human_reviews.py"
echo 'Preflight completed. No migration, DB write, Docker restart, or gateway replacement was performed.'
