#!/usr/bin/env bash
# Hostinger Terminal; hides the token while it is pasted.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f private/config.php && -f private/.htaccess ]] || { echo 'Run in the installed Masakhane site. Existing config not found.' >&2;exit 2; }
read -r -s -p 'Paste the DigitalOcean QC review token (not your login password): ' MASAKHANE_QC_REVIEW_TOKEN
echo
export MASAKHANE_QC_REVIEW_TOKEN
php tools/configure_qc_token.php
unset MASAKHANE_QC_REVIEW_TOKEN
php tools/check_qc_connection.php
