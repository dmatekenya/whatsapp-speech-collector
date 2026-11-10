# Deployment — Hostinger + DigitalOcean

**Do not upload or post credentials, SSH private keys, recorded voices or private `.env` files.**

## A. BACK UP FIRST

1. In Hostinger create a backup of **all** files for `masakhane.zazoai.com` and export the `u739872438_masakhane` database from phpMyAdmin.
2. Confirm the production collector operator has a verified DigitalOcean PostgreSQL and gateway container backup. Existing production audio and WhatsApp collection must continue uninterrupted.
3. Keep Hostinger `private/config.php`, `private/profile_images`, the accounts and all MySQL tables. The package intentionally does not contain your configuration or original data.

## B. Hostinger: complete dashboard replacement

Upload `Masakhane_Hostinger_Complete_Online_0.0.0.1.zip` to the current dashboard directory. In Hostinger's protected terminal:

```bash
cd ~/domains/zazoai.com/public_html/masakhane
unzip -o Masakhane_Hostinger_Complete_Online_0.0.0.1.zip
php tools/upgrade_online.php
php -l index.php
php -l app/reviews.php
php -l app/admin_pages.php
```

`tools/upgrade_online.php` **preserves** existing accounts, assignments, recordings and prior reviews and only adds the review-confirmation fields and index if missing. It does not change the production database. After validation, remove the uploaded ZIP from the public website folder.

Open `https://masakhane.zazoai.com`, sign in as admin and press `Ctrl+F5`. Confirm: Supervisors → select a supervisor → Manage assignments. Settings shows the default 5-second visible-browser check. Review pages show a clear warning that official writes are not activated yet.

## C. DigitalOcean: confirm business rules BEFORE enabling QC write-back

The previous gateway remains suitable for audio and read-only sync. The new Python gateway is a separate complete replacement to be installed only with production-owner approval.

The new `database/OWNER_REVIEW_SCHEMA.sql` contains the *additional* PostgreSQL audit table. The production owner must review the SQL and confirm that `public.recordings` is the correct official QC state, the allowed transitions, and whether existing WhatsApp feedback or processing is triggered by `qc_status`. Do not directly update collector status until this is confirmed. Allocate a **separate least-privilege writer**; never reuse the `sc_readonly` database account or the production superuser.

Upload `Masakhane_DigitalOcean_Gateway_Reviewed_Writeback_0.0.0.1.zip` to `/home/stewart/` using the authorized SSH account. In DigitalOcean SSH:

```bash
cd /home/stewart
mkdir -p masakhane-qc-gateway-release
python3 -m zipfile -t Masakhane_DigitalOcean_Gateway_Reviewed_Writeback_0.0.0.1.zip
python3 -m zipfile -e Masakhane_DigitalOcean_Gateway_Reviewed_Writeback_0.0.0.1.zip masakhane-qc-gateway-release
sudo bash /home/stewart/masakhane-qc-gateway-release/scripts/diagnose_audio_readonly.sh
```

Once the production owner has approved the DB audit schema, QC transitions and writer privileges, create the dedicated writer role and apply the reviewed SQL in the production PostgreSQL administration session. Keep its password private.

Safely edit **only** `/opt/masakhane-supervision-gateway/private.env` using `sudoedit` and append:

```ini
SUPERVISION_WRITES_ENABLED=1
SUPERVISION_REVIEW_TOKEN=<long separately generated private secret 48+ characters>
SUPERVISION_WRITE_DSN=<dedicated least-privilege PostgreSQL writer connection>
```

Do not expose this file. The token should be different from the existing read-only `SUPERVISION_GATEWAY_TOKEN`. Example secure generation on DigitalOcean: `openssl rand -hex 32`. Do not share the generated result in chat or screenshots.

With owner approval, upgrade **only** the isolated gateway. The existing installer makes a rollback container and tests audio file readability; original collector, Caddy, other services and audio directory are unchanged:

```bash
sudo bash /home/stewart/masakhane-qc-gateway-release/scripts/repair_gateway_audio.sh --owner-approved
```

The gateway remains private behind the existing HTTPS Caddy route. Do not open new public ports or make the PostgreSQL writer reachable externally.

## D. Connect Hostinger to the separate writer credential

In the **existing protected Hostinger config** (`private/config.php`), keep every current key unchanged, and add only this PHP array member:

```php
'review_api_token' => '<same private SUPERVISION_REVIEW_TOKEN configured on DigitalOcean>',
```

Do **not** replace the whole `config.php`, paste a token into a browser-address URL, or put it in `public_html` files other than the protected PHP config. The Hostinger PHP application sends the write token only server-to-server over the existing verified HTTPS connector.

## E. Verification

1. Online Integration Status must show `Production QC write-back: Ready — requires confirmation` (only after successful separate write-status permission test).
2. Verify recording #181 (or another authorized stored recording) plays in the browser.
3. Create a supervisor account. Assign some real contributor IDs. Sign in as supervisor and ensure only those contributors and recordings appear. Attempt to open an unassigned recording must result in access denied / not found.
4. For one operator-approved real recording, after confirming how the collector handles QC, submit a **documented** test review. Observe `Confirmed` only when DigitalOcean responds. Refresh the Production Voice list and verify the official QC state changed on the remote collector. Then check the online audit and the DigitalOcean audit table. Do not test by indiscriminately approving actual study data.
5. Confirm 500-hour collection progress changes only for production-accepted, real, nonduplicate stored recordings.
6. When the browser is visible, watch the online live sync label. For unattended collection, configure an appropriate Hostinger cron task using CLI PHP (`cron/sync.php`) in **Advanced → Cron Jobs**, at the shortest period allowed by the plan. Hostinger cron schedules use UTC. The dashboard is not dependent on the local computer.

## F. Rollback

Hostinger: restore the file and database backups from step A. Never delete the preserved `private/config.php` or profile images. If the new PHP app is reverted, also revert the gateway write feature or leave `SUPERVISION_WRITES_ENABLED=0` so legacy portal forms cannot submit production actions.

DigitalOcean: the gateway upgrade command prints its backup container name. The script's printed rollback command restores only the previous gateway container. Do not delete or recreate the original collector database or media.

## Safety / status

This is an **offline-tested replacement package**. It has not been installed on your live services from this conversation. Official QC is not confirmed to be write-enabled until steps C–E succeed. Version remains `0.0.0.1`.
