MASAKHANE — ACTION LOG FULL PHP FILE REPLACEMENT
================================================
Version: 2026-11-10
Target: Hostinger online dashboard at masakhane.zazoai.com

WHAT IS INCLUDED
- app/admin_pages.php : complete replacement of the PHP file, preserving supervisor management, assignment management, profile, settings and integration pages.
- tools/action_log_diagnostic.php : terminal-only, read-only compatibility tests.

FIXES IN THE REPLACEMENT
- Action Log avoids optional undefined helper dependencies for filters and prompt labels.
- Verifies database column availability at runtime; gives a useful error instead of taking down the entire dashboard if reporting schema is incompatible.
- Retains last 200 account/access and sync events, supervisor/contributor/prompt breakdown, CSV export and Malawi date filters.
- Counts only CONFIRMED approvals/rejections as production decisions, while preserving historical draft/failed/legacy escalations for audit.
- Keeps admin-only access. Does not modify recordings, accounts, QC write-back or private API tokens.

HOSTINGER INSTALLATION (run in HOSTINGER terminal, NOT Windows PowerShell)
1. Open the existing site directory:
cd /home/u739872438/domains/zazoai.com/public_html/masakhane || exit 1

2. Make a backup BEFORE extracting (backup is outside public_html):
mkdir -p "$HOME/masakhane_actionlog_backup"
cp -p app/admin_pages.php "$HOME/masakhane_actionlog_backup/admin_pages.php.$(date +%Y%m%d_%H%M%S)"

3. Upload Masakhane_ActionLog_FULL_Replacement_20261110.zip to this masakhane directory, then extract:
python3 -m zipfile -e Masakhane_ActionLog_FULL_Replacement_20261110.zip .

4. Verify syntax and existing DB compatibility:
php -l app/admin_pages.php
php -l tools/action_log_diagnostic.php
php tools/action_log_diagnostic.php

5. Visit https://masakhane.zazoai.com/index.php?page=audit in a fresh browser session.
Use an admin account. Test Reviews, Account & access, and Sync history tabs.

6. DELETE THE ZIP AND DIAGNOSTIC TOOL FROM THE WEBROOT AFTER TESTING:
rm -f Masakhane_ActionLog_FULL_Replacement_20261110.zip tools/action_log_diagnostic.php

ROLLBACK IF NEEDED
List the backup files created under $HOME/masakhane_actionlog_backup.
Copy the desired admin_pages.php.TIMESTAMP back to app/admin_pages.php:
cp -p "$HOME/masakhane_actionlog_backup/admin_pages.php.TIMESTAMP" app/admin_pages.php
php -l app/admin_pages.php

IMPORTANT
- This fixes Action Log reporting only. It does NOT change DigitalOcean QC, audio, permissions, approvals, rejections or production database tables.
- No secrets, database passwords or API tokens are included.
- A full-site replacement is not possible from the five uploaded files; do not overwrite unrelated dashboard files.
- If the diagnostic says the reporting schema is missing required fields, send only the column names and diagnostic output, not any credentials or personal information.
