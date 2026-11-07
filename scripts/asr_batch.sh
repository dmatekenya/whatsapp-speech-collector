#!/usr/bin/env bash
# =========
# ASR BATCH
# =========
# Runs scripts/asr_batch.py (transcribe recordings on Modal); options are passed on:
#
#   ./scripts/asr_batch.sh --dry-run
#   ./scripts/asr_batch.sh --include-test --participant P0006
#
# Where it runs is chosen by ASR_BATCH_RUNNER (never guessed):
#   uv      (default) on the laptop: uv run python -m scripts.asr_batch
#   docker  on the droplet: inside the running api container, like the other
#           scripts (docker compose exec api ...), which has data/ mounted and
#           the settings from .env (MODAL_TOKEN_ID / MODAL_TOKEN_SECRET).
#           Set it in ~/.bashrc on the droplet and in the crontab line.
#
# Hourly from cron on the droplet (it only runs at the slots in config/asr_batch.yaml):
#   5 * * * * ASR_BATCH_RUNNER=docker /home/deploy/whatsapp-speech-collector/scripts/asr_batch.sh --scheduled >> /home/deploy/whatsapp-speech-collector/data/asr_batches/cron.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."

# ======
# RUNNER
# ======
runner="${ASR_BATCH_RUNNER:-uv}"
case "$runner" in
    uv)
        if ! command -v uv >/dev/null 2>&1; then
            echo "uv not found. On the droplet, run with ASR_BATCH_RUNNER=docker." >&2
            exit 2
        fi
        exec uv run python -m scripts.asr_batch "$@"
        ;;
    docker)
        if [ -z "$(docker compose ps --status running --quiet api 2>/dev/null)" ]; then
            echo "The api container isn't running; start it with ./scripts/deploy.sh (or docker compose up -d)." >&2
            exit 2
        fi
        # -T: no terminal, so it works from cron too.
        exec docker compose exec -T api python -m scripts.asr_batch "$@"
        ;;
    *)
        echo "ASR_BATCH_RUNNER must be uv or docker, not '$runner'." >&2
        exit 2
        ;;
esac
