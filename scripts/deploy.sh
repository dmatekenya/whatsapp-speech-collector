#!/usr/bin/env bash
# ======
# DEPLOY
# ======
# Run on the droplet, from anywhere: pulls the latest code, rebuilds the image,
# applies migrations (the migrate service) and restarts what changed.
# Safe to re-run; it's also how you update later.
#
#   ./scripts/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

# ======
# CHECKS
# ======
if [ ! -f .env ]; then
    echo "No .env here. Create it from .env.production.example (see docs/deploy.md)." >&2
    exit 1
fi
if ! grep -Eq '^DOMAIN=[^[:space:]]+' .env; then
    echo "DOMAIN isn't set in .env; Caddy needs it for HTTPS." >&2
    exit 1
fi
# The bind-mounted data folders must exist (and belong to you, not root).
mkdir -p data/audio data/participants data/prompts data/asr_batches data/qc backups

# ======
# UPDATE
# ======
git pull --ff-only
docker compose build --pull
docker compose up -d --remove-orphans

# =====
# CHECK
# =====
sleep 5
docker compose ps
echo
echo "Migration output:"
docker compose logs --no-log-prefix --tail 5 migrate
# Remove image layers left over from previous builds.
docker image prune -f >/dev/null
echo
echo "Done. Check https://$(grep -E '^DOMAIN=' .env | head -1 | cut -d= -f2-)/health"
