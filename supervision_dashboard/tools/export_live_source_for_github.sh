#!/usr/bin/env bash
# Export a SOURCE-ONLY snapshot of the running Masakhane PHP dashboard for GitHub.
# Does not modify the live site; does not copy config, private data, audio, avatars or backups.
set -euo pipefail
umask 077
ROOT="${1:-$HOME/domains/zazoai.com/public_html/masakhane}"
if [[ ! -f "$ROOT/index.php" || ! -d "$ROOT/app" ]]; then
  echo 'ERROR: Dashboard root not found. Pass the full path as the first argument.' >&2
  exit 1
fi
TS=$(date +%Y%m%d-%H%M%S)
DEST="$HOME/Masakhane_LIVE_Source_Export_$TS"
OUT="$DEST/supervision_dashboard"
mkdir -p "$OUT"
cp -p "$ROOT/index.php" "$OUT/index.php"
[[ -f "$ROOT/.htaccess" ]] && cp -p "$ROOT/.htaccess" "$OUT/.htaccess"
# Explicit file extensions + explicitly authorized source directories only.
# Exclude root install-v*.php, temp ZIPs, logs, and ALL runtime private contents.
for dir in app assets cron docs tools dashboard-demographics; do
  [[ -d "$ROOT/$dir" ]] || continue
  while IFS= read -r -d '' filepath; do
    rel="${filepath#"$ROOT/"}"
    case "$rel" in
      *'.env'*|*'config.php'|*'qc_review_token.php'|*'.bak'*|*'backup'*|*'secret'*|*'token.php'|*'credential'*|*'password'* ) continue ;;
    esac
    mkdir -p "$OUT/$(dirname "$rel")"
    cp -p -- "$filepath" "$OUT/$rel"
  done < <(find "$ROOT/$dir" -type f \( -name '*.php' -o -name '*.js' -o -name '*.css' -o -name '*.md' -o -name '*.json' -o -name '*.txt' -o -name '.htaccess' \) -print0)
done
# Placeholder only; never export the real private/config.php.
mkdir -p "$OUT/private"
printf 'Require all denied\n' > "$OUT/private/.htaccess"
cat > "$OUT/private/config.example.php" <<'PHP'
<?php
/** Example only. Create a real private/config.php securely on Hostinger. */
declare(strict_types=1);
return [
 'db_host'=>'localhost','db_port'=>3306,'db_name'=>'','db_user'=>'',
 'db_pass'=>'','api_url'=>'','api_token'=>'','api_timeout'=>25,
 'review_api_token'=>'',
];
PHP
cat > "$OUT/.gitignore" <<'IGNORE'
/private/config.php
/private/profile_images/
/private/*.env
/private/.env*
/private/qc_review_token.php
/private/*.log
*.log
*.bak
*.zip
*.tar.gz
*.sql
*.xlsx
*.csv
/data/
/recordings/
/uploads/
/storage/
/backups/
.env
.env.*
*.pem
*.key
IGNORE
cat > "$DEST/REVIEW-BEFORE-GITHUB.txt" <<'TXT'
SOURCE EXPORT FROM LIVE HOSTINGER. NOT A DATABASE OR CONFIG BACKUP.
Please review every file for hard-coded credentials or personal data before
committing. This script intentionally omitted private/config.php, avatars,
recordings, uploads, SQL dumps, and tokens. Scripts and application files
could still contain secrets if they were hardcoded manually.

Do not assume this source export is a deployable backup of your whole website.
TXT
if command -v zip >/dev/null 2>&1; then
  (cd "$DEST" && zip -qr "${DEST}.zip" supervision_dashboard REVIEW-BEFORE-GITHUB.txt)
  echo "SOURCE ZIP READY: ${DEST}.zip"
else
  tar -C "$DEST" -czf "${DEST}.tar.gz" supervision_dashboard REVIEW-BEFORE-GITHUB.txt
  echo "SOURCE TAR.GZ READY: ${DEST}.tar.gz"
fi
echo "Source folder: $OUT"
echo 'Review the archive locally for sensitive information before GitHub upload.'
