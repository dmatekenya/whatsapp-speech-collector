# Hostinger — FULL replacement Ver. 0.0.0.1

Contains the full PHP application, application CSS/JS, audio and API connector, supervisor access, reviewer history, filtering and aggregations. This replaces the whole source distribution; it does **not** reset existing database data.

Do not delete `private/config.php` or any existing `private/profile_images` files. They are intentionally not included. Never upload the DigitalOcean ZIP here.

Run in the current Masakhane Hostinger folder:

```bash
php -l index.php
php -l app/admin_pages.php
php tools/upgrade_online.php
```

For QC activation the production owner must first authorize the separate DigitalOcean QC writer, then run `bash tools/configure_qc_token.sh` here and `php tools/check_qc_connection.php`. Do not bypass the connectivity check: production updates must be verified at the actual source.
