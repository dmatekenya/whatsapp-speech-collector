"""Create .env.production for the droplet from .env.production.example.

    python3 scripts/make_env_production.py --dry-run     # report only
    python3 scripts/make_env_production.py               # write .env.production
    python3 scripts/make_env_production.py --force       # replace it (keeps .env.production.bak)

The template's keys, order, comments, blank lines and headers are kept. Only
the Twilio settings in COPY_FROM_LOCAL are copied from your local .env; new
random values are generated for GENERATE; SET gets fixed values; everything
else keeps the template's value. Only key names and what happened to each are
printed, never a value. The written file is readable by you only (600).
"""

import argparse
import os
import re
import secrets
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ========================
# WHAT HAPPENS TO EACH KEY
# ========================
# Copied from the local .env. These are the Twilio settings the app reads
# (src/config.py). There's no messaging service SID: the app sends from
# TWILIO_WHATSAPP_NUMBER. TWILIO_VALIDATE_SIGNATURE isn't copied on purpose:
# it must stay true on the server (the template's value).
COPY_FROM_LOCAL = (
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_WHATSAPP_NUMBER",
    "TWILIO_DELETE_MEDIA",
)

# New random values (secrets.token_urlsafe(32)) every time the file is made.
GENERATE = ("MEDIA_SIGNING_SECRET", "POSTGRES_PASSWORD")

# Fixed values for the droplet. The Caddyfile reads the domain from DOMAIN.
SET = {
    "DOMAIN": "bot.zazoai.com",
    "PUBLIC_BASE_URL": "https://bot.zazoai.com",
}

# Never copied from the local .env, whatever COPY_FROM_LOCAL says: they must
# differ on the server (checked when the script starts).
NEVER_COPY = re.compile(
    r"(PASSWORD|SECRET|DATABASE_URL|REDIS_URL|PUBLIC_BASE_URL|_ROOT$|PATH|DIR|APP_ENV|ENVIRONMENT|LOG_LEVEL|POSTGRES_)"
)

PLACEHOLDER = re.compile(r"change|xxx|your|<", re.IGNORECASE)
KEY_LINE = re.compile(r"^(?P<lead>\s*(?:export\s+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)(?P<eq>\s*=)(?P<value>.*)$")


# =======
# PARSING
# =======
def read_env(path: Path) -> dict[str, str]:
    """KEY=VALUE pairs from an env file, values exactly as written (quotes kept)."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = KEY_LINE.match(line)
        if match:
            values[match["key"]] = match["value"].strip()
    return values


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def looks_like_placeholder(value: str) -> bool:
    return _unquote(value).strip() == "" or PLACEHOLDER.search(value) is not None


# ========
# BUILDING
# ========
def build(template_lines: list[str], local: dict[str, str]) -> tuple[list[str], list[tuple[str, str]], dict[str, str]]:
    """The new file's lines, a report of (key, what happened), and the final values."""
    generated = {key: secrets.token_urlsafe(32) for key in GENERATE}
    out: list[str] = []
    report: list[tuple[str, str]] = []
    final: dict[str, str] = {}

    for line in template_lines:
        match = KEY_LINE.match(line) if not line.lstrip().startswith("#") else None
        if match is None:
            out.append(line)
            continue
        key, value = match["key"], match["value"].strip()
        if key in generated:
            value, status = generated[key], "generated"
        elif key in SET:
            value, status = SET[key], "set"
        elif key in COPY_FROM_LOCAL:
            if key in local:
                value, status = local[key], "copied"
            else:
                status = "missing in .env (template default kept)"
        else:
            status = "template default"
        final[key] = value
        report.append((key, status))
        out.append(f"{match['lead']}{key}{match['eq']}{value}")

    # A DATABASE_URL (if the template has one) must use the new password.
    if "DATABASE_URL" in final and "POSTGRES_PASSWORD" in generated:
        new_url = re.sub(r"(://[^:/@]+:)[^@]*(@)", rf"\g<1>{generated['POSTGRES_PASSWORD']}\g<2>", final["DATABASE_URL"])
        if new_url != final["DATABASE_URL"]:
            final["DATABASE_URL"] = new_url
            out = [
                f"{m['lead']}DATABASE_URL{m['eq']}{new_url}" if (m := KEY_LINE.match(line)) and m["key"] == "DATABASE_URL" else line
                for line in out
            ]
            report = [(k, "updated to match the new POSTGRES_PASSWORD" if k == "DATABASE_URL" else s) for k, s in report]

    for key in COPY_FROM_LOCAL:
        if key not in final:
            report.append((key, "not in the template (not written)"))
    return out, report, final


# =======
# WRITING
# =======
def write_private(path: Path, text: str) -> None:
    """Write atomically with permissions 600 from the start."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    os.chmod(path, 0o600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    parser.add_argument("--force", action="store_true", help="replace an existing output (backed up to .bak first)")
    parser.add_argument("--env", type=Path, default=ROOT / ".env", help="local env file to copy Twilio settings from")
    parser.add_argument("--template", type=Path, default=ROOT / ".env.production.example")
    parser.add_argument("--output", type=Path, default=ROOT / ".env.production")
    args = parser.parse_args(argv)

    unsafe = [key for key in COPY_FROM_LOCAL if NEVER_COPY.search(key)]
    if unsafe:
        parser.error(f"COPY_FROM_LOCAL includes keys that must never be copied: {', '.join(unsafe)}")
    if not args.template.is_file():
        parser.error(f"template not found: {args.template}")
    if not args.env.is_file():
        parser.error(f"local env file not found: {args.env}")
    if args.output.resolve() in (args.template.resolve(), args.env.resolve()):
        parser.error("--output must not be the template or the local .env")
    if args.output.exists() and not args.force and not args.dry_run:
        parser.error(f"{args.output.name} already exists; use --force to replace it (a .bak copy is kept)")

    template_lines = args.template.read_text(encoding="utf-8").splitlines()
    lines, report, final = build(template_lines, read_env(args.env))

    width = max(len(key) for key, _ in report)
    print(f"{'DRY RUN: ' if args.dry_run else ''}{args.output.name} from {args.template.name} (+ {args.env.name})")
    for key, status in report:
        print(f"  {key:<{width}}  {status}")
    generated = {key for key, status in report if status == "generated"}
    placeholders = [
        key for key, value in final.items() if key not in generated and looks_like_placeholder(value)
    ]
    if placeholders:
        print("\nStill empty or placeholder-like (fill these in before deploying):")
        for key in placeholders:
            print(f"  {key}")
    else:
        print("\nNo empty or placeholder-like values.")

    if args.dry_run:
        print("\nDry run: nothing written.")
        return 0
    if args.output.exists():
        backup = args.output.with_name(args.output.name + ".bak")
        shutil.copy2(args.output, backup)
        os.chmod(backup, 0o600)
        print(f"\nBacked up the existing file to {backup.name}")
    write_private(args.output, "\n".join(lines) + "\n")
    print(f"\nWrote {args.output} (permissions 600).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
