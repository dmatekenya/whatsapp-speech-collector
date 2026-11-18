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
if [ "$#" -eq 1 ]; then
    git fetch --tags
    git switch --detach "refs/tags/$1"
elif [ "$#" -eq 0 ]; then
    if [ "$(git branch --show-current)" != main ]; then
        echo "Check out main before deploying." >&2
        exit 1
    fi
    git fetch origin main --tags
    git merge --ff-only origin/main
    if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
        echo "Local main has commits absent from origin/main." >&2
        exit 1
    fi
else
    echo "Usage: $0 [release-tag]" >&2
    exit 2
fi
export APP_GIT_COMMIT="$(git rev-parse HEAD)"
APP_VERSION="$(git describe --tags --match 'v[0-9]*' --exact-match HEAD 2>/dev/null || true)"
if [ -n "$APP_VERSION" ]; then
    export APP_VERSION="$APP_VERSION"
else
    export APP_VERSION="$(sed -n 's/^APP_VERSION = "\([^"]*\)"$/\1/p' src/version.py)"
    test -n "$APP_VERSION"
fi
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
