# Masakhane Tier 3 — owner review (Dunstan contract, 2026-11-10)

## Current safety state — DO NOT CHANGE until owner signs off

- `SUPERVISION_WRITES_ENABLED=0` (writeback default off).
- `masakhane_qc_writer` is `NOLOGIN`; no commands in this package re-enable it.
- Do not install the superseded `9b4d0e72c1f0_supervisor_human_reviews.py` migration tested previously. **It is deliberately absent from this release** because it conflicts with Dunstan's contract.
- Dunstan owns creation of `qc_human_reviews`, recording QC columns, triggers, database grants, trigger transition rules, and Alembic revision ordering. No arbitrary migration has been shipped here.
- No live Docker, Caddy, PostgreSQL, SSH or security configuration was changed by preparing this package. Existing audio routes remain necessary.

## API contract implemented (source prepared for owner review)

`POST /v1/supervision/reviews` (trusted server-to-server Hostinger backend only):

```json
{
  "operation_key":"UUIDv4",
  "recording_id":896,
  "contributor_id":"P0026",
  "reviewer_id":"4",
  "reviewer_name":"Supervisor Gloria",
  "decision":"reject",
  "reason_code":"noise",
  "note":"Unclear due to background noise",
  "expected_qc_tier3":null
}
```

Notes:
- `decision`: exactly `accept` or `reject`.
- `reason_code`: required for reject. Proposed values: `wrong_sentence`, `noise`, `not_own_voice`, `unclear`, `not_chichewa`, `other`. Confirm additions before freezing the owner DB CHECK constraint.
- `note`: optional, except required for `reason_code='other'`.
- `operation_key`: UUID unique for idempotent network retries; new UUID for each new decision/reversal.
- `expected_qc_tier3`: `null`, `accept` or `reject` from the most recently synced DigitalOcean state. Do NOT use arrival `qc_status` for this.
- `reviewer_id` and `reviewer_name` are supplied by the **authenticated Hostinger server-side session**. Contributor access is checked in Hostinger's backend, not delegated to untrusted browser values.
- Gateway uses the dedicated writer DSN for INSERT/SELECT into `qc_human_reviews` only; read-only DSN verifies recording/contributor, current `qc_tier3`, and media eligibility.
- Gateway serializes its own submissions per recording with a PostgreSQL advisory lock. For **cross-service atomic conflict protection**, Dunstan's owner trigger MUST lock `recordings` and compare `recordings.qc_tier3` against transaction-local `current_setting('masakhane.expected_qc_tier3', true)` (`__NULL__` represents SQL NULL); reject mismatch. A trigger presence check alone is not proof of correct conflict handling.
- Owner trigger must assign `qc_tier3`, `qc_tier3_reason`, `qc_final`, and update `recordings.updated_at` atomically. Gateway never directly modifies `recordings`.
- Owner review trigger must be append-only, using row creation timestamp and ID to determine current decision; operation_key UNIQUE. No UNIQUE(recording_id).
- If owner chooses a different CAS/trigger protocol, adjust gateway to their approved protocol BEFORE enabling writes.
- Owner should account for how bigint IDs are generated without granting excessive sequence permissions (identity or restricted USAGE).
- Do not include participant names, phone numbers, WhatsApp messages, or sensitive metadata in gateway responses.

## Paginated read-only sync

- GET `/v1/supervision/changes?after=UTC_TIMESTAMP&after_id=INT&until=UTC_TIMESTAMP&limit=500`.
- Sorted by `(recordings.updated_at, recordings.id)` with a fixed UTC upper watermark and a bound of at most 500 rows per page. No 10,000-row limit in the new sync flow.
- GET `/v1/supervision/summary` shows collected (`qc_status != 'rejected'` for stored clips) versus usable (`qc_final IN ('gold','silver')`), plus bronze and rejected separately.
- GET `/v1/supervision/revision` is a small change probe. Hostinger commits each metadata page and cursor in one MySQL transaction; replay is safe because it upserts by recording id.
- No automatic deletion of old Hostinger recordings during sync. Metadata cache is not a substitute for source or audio.
- `qc_results.band_reason` is read only when source fields and grants exist. Confirm the owner's live `qc_results` structure before relying on that field.
- A grant to new final columns after the pilot triggers a one-time complete resync of cached records.

## Owner acceptance requirements

1. Review production migration in git, not directly on droplet. The previously-tested release migration is incompatible and intentionally excluded.
2. Review actual current Caddyfile route (local droplet edit) and commit the matching route into your branch; do not guess a new route or overwrite the existing route. No actual Caddyfile was provided in the source archive.
3. Verify append-only audit; no writes to Tier 1 `recordings.qc_status`; no update/delete permission on human-review history; no direct writer permission on `recordings`.
4. Test a reversal and conflicting simultaneous writes against owner-trigger CAS semantics in an **isolated restored database**; verify exact current `qc_tier3`, `qc_final` and the audit ledger.
5. Verify authorized supervisor access, unassigned contributor rejection, errors not shown as success, retried UUID, action log and automatic next recording.
6. Verify 10,001+ recordings via cursor pages, including updates to an older recording after original import.
7. Confirm Caddy proxy, Docker private network, read-only audio mount, HTTPS auth and rollback plan.
8. After owner signoff, perform migration, gateway rollout and Hostinger schema upgrade in the approved order with fresh backups.

## Production cleanup owner requested (manual, after signoff)

- Once Hostinger safely holds its review token, delete `/opt/masakhane-supervision-gateway/hostinger-review-token-TRANSFER-AND-DELETE.txt`; do not display or copy token value into chat or git.
- Stop/remove `masakhane-supervision-gateway-fourtypes-test` after Dunstan approves that production-container change. Main `masakhane-supervision-gateway` is separate and must stay running.
- Report whether SSH was opened on port 22222. Check read-only with `sudo ss -ltnp | grep ':22222'` and `sudo sshd -T | grep '^port '`. Do not change SSH networking in this package.

## Not tested in this environment

Live Docker/Caddy, HTTPS, Hostinger MySQL migrations, owner-created PostgreSQL triggers, simultaneous database reviews, large-load sync. Syntax and offline tests do NOT replace a staged end-to-end integration test.
