# syntax=docker/dockerfile:1

# =========================
# WHATSAPP SPEECH COLLECTOR
# =========================
# One image for the API, the worker and one-off commands (migrations, imports).
# Built on the droplet (x86_64) as well as on an arm64 Mac: nothing here is
# architecture-specific.
#
#   docker compose build                    # production image
#   INSTALL_DEV=true docker compose build   # also pytest etc. (local testing)

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # The app runs from /app (not installed as a package): src, scripts,
    # config and messages are all found relative to it.
    PYTHONPATH=/app \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/opt/venv/bin:$PATH"

# ===============
# SYSTEM PACKAGES
# ===============
# ffmpeg/ffprobe convert voice notes to WAV. Pillow, psycopg and asyncpg ship
# as binary wheels with their own image and database libraries, so nothing
# else is needed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.14 /uv /usr/local/bin/uv

WORKDIR /app

# ============
# DEPENDENCIES
# ============
# Exactly what uv.lock pins (--frozen). --no-install-project: the project's
# version comes from git tags (hatch-vcs), which aren't in the build context;
# the code is run from /app instead.
ARG INSTALL_DEV=false
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$INSTALL_DEV" = "true" ]; then \
        uv sync --frozen --no-install-project; \
    else \
        uv sync --frozen --no-install-project --no-dev; \
    fi

# ===
# APP
# ===
# A non-root user. APP_UID should match the owner of ./data on the host (the
# bind mount), usually 1000 for the first user on Ubuntu.
ARG APP_UID=1000
RUN useradd --create-home --uid "${APP_UID}" app \
    && mkdir -p /app/data \
    && chown app:app /app /app/data

COPY --chown=app:app . .
USER app

EXPOSE 8000
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
