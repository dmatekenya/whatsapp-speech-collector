# Masakhane Supervision Dashboard — GitHub-ready source

**Source snapshot:** 12 November 2026. Built from the supplied `supervision_dashboard.zip` and updated with the documented contributor demographic charts. **This is not a verified export of the running Hostinger instance.** Other changes made directly on the server after the supplied archive may not be present.

## Features

- PHP/MySQL supervision portal hosted on Hostinger, using a DigitalOcean PostgreSQL speech collector via an authenticated HTTPS production gateway.
- Admin users can manage supervisors, assignments, full recording catalogs, QC reports, notifications and action logs; supervisors see only their assigned contributors and recordings.
- Production audio playback, date/prompt filters, Accept/Reject human-review workflow with controlled official QC write-back when specifically enabled and approved.
- Four prompt-type duration rings, usable gold/silver hours, 500-hour target, aggregate QC statuses, recording calendar and searchable recording lists.
- Live browser checks with revision-based redraw; the initial configuration is 5 seconds. End-to-end freshness also depends on the authorized production gateway and Hostinger cron schedule.
- **New in this repository snapshot:** Contributor Demographics on the authenticated **admin Overview**: gender, country, five-year age group and first language. The charts use pre-aggregated public-safe data published by ZaZoAI; country is district-derived when no verified country exists. The live chart endpoint returns no contributor names or personal data.

## Repository layout

- `index.php` — routing, login, controlled live refresh and dashboard entry.
- `app/` — rendering, workflow, authorization, integration, reports and review controls.
- `assets/` — frontend CSS and JavaScript.
- `dashboard-demographics/` — authenticated public-safe aggregate proxy and four charts.
- `cron/` — optional background sync hook.
- `private/config.example.php` — **example only**; live `config.php` must be created privately on Hostinger.
- `docs/` — deployment, gateway-contract and security reference material.

## GitHub usage

Unpack the ZIP, open `supervision_dashboard/` as the repository root and commit its contents to your `supervisor-dashboard` branch (or create a new repository). The `.gitignore` excludes secret/private runtime data. Before pushing, review `git status` and run a secret scan.

Example from **Windows PowerShell**:

```powershell
cd C:\Users\Chikomola\speech\whatsapp-speech-collector
# Option A: if this directory is your collector project, keep the dashboard in its own folder:
# Copy the entire extracted `supervision_dashboard` directory into this repository.
git status
git add supervision_dashboard
git diff --cached --stat
git commit -m "Update Masakhane supervision dashboard and demographic charts"
git push origin supervisor-dashboard
```

If the dashboard lives in its *own* Git repository, put these files at that repository root instead of a nested directory.

## Deployment and security

This GitHub ZIP is **source code**, not a live database dump or a complete copy of production. It excludes Hostinger credentials, QC tokens, private avatars, media and participant data. It does **not** install a database, change DigitalOcean PostgreSQL, perform a QC write-back or update your live website automatically.

To deploy the updated application to Hostinger, back up its current files and MySQL database, compare server changes against the repository, preserve `private/config.php` and the private media folders, and review `docs/HOSTINGER_DEPLOYMENT_GUIDE.md`. Never overwrite those with examples. Official QC actions remain subject to the current gateway's authorization and deployment state.

**Compatibility:** PHP 8.2+ is recommended, MySQL/MariaDB via PDO, HTTPS, and PHP cURL (or SSL-capable file streams) for the demographic proxy.

## November 2026 source update

See [`docs/updates-2026-11-12.md`](docs/updates-2026-11-12.md) for the maintained changelog and collector gateway alignment requirements. This repository covers the Hostinger supervision application only; collector-side changes must be made and reviewed in the DigitalOcean speech-collector repository.
