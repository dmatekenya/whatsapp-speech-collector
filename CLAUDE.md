# whatsapp-speech-collector

## Environment
- A real `.env` already exists in the repo root. Never read, print, create or modify it,
  and don't tell me to copy `.env.example` to `.env`.
- When a change adds or removes settings, update `.env.example` (names and placeholder
  values only) and list the new variable names in your summary. Otherwise leave it alone.

## Local services
- Postgres 17 and Redis run via Homebrew services; Docker is not available on this machine.
- Run the app with `uv run`. Migrations with Alembic.

## Conventions
- Never log full phone numbers or media URLs; use mask_phone.
- Don't commit or push; I review first.


## Section headers
- Section headers in code and config files (Python, YAML, shell) use a banner comment:
  a rule line of `=`, the title in CAPITALS on its own line, then a matching rule line.
- The rule lines are exactly as long as the title, so they line up with it:

      # =======
      # SESSION
      # =======

      # =====================
      # PROBLEMS WITH A RECORDING
      # =====================   <- wrong: too short

      # =========================
      # PROBLEMS WITH A RECORDING
      # =========================

- Never inline styles like `# ---- Title ----`.
- When editing a file, update any existing headers in it to this style.