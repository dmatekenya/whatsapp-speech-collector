# Supervision dashboard and QC: what to change

For: Stewart (SC). From: Dunstan. Date: 2026-11-10.
Suggested repo path: `docs/plans/supervision-qc-contract.md`.

Thank you for the work on the dashboard and gateway. There's a lot of careful thinking in it:
token-protected gateway, audio mounted read-only, supervisors limited to their own contributors,
login rate limiting, an append-only audit table, and writes off by default. Keeping names and phone
numbers off Hostinger is a good call.

Since you built it, we've agreed how quality checks fit together (`docs/methodology.md`, section 8).
In short:

| Tier | Who | Where it's recorded | Values |
|---|---|---|---|
| 1. Arrival checks | the bot | `recordings.qc_status`, `qc_reason` | `pending`, `accepted`, `needs_review`, `rejected` |
| 2. Daily ASR checks | the ASR batch | `qc_results` (history), `recordings.qc_tier2` | 1 (worst) to 4 (best) |
| 3. Human review | supervisors, via your dashboard | `qc_human_reviews` (history), `recordings.qc_tier3` | `accept`, `reject` |
| Final label | database triggers | `recordings.qc_final` | `bronze`, `silver`, `gold`, `rejected` |

Below is what needs to change, in order of priority.

## 1. Now: safety and process

- **Write-back stays off.** On 2026-11-10 I blocked the writer account
  (`ALTER ROLE masakhane_qc_writer NOLOGIN`) and set `SUPERVISION_WRITES_ENABLED=0`. Please don't
  re-enable either. The audit table was empty, so no decisions were written and nothing needs
  restoring. Read-only viewing and audio still work, so the dashboard can be used for the pilot.
- **`qc_status` is tier 1 and must never be changed** by the dashboard or the gateway. The bot's
  progress counts and arrival rules depend on it.
- **Every change on the droplet goes through a branch I review first:** containers, Caddyfile,
  database roles, tables and views. It's production, and I need to know everything running there.
- Please also:
  - delete `/opt/masakhane-supervision-gateway/hostinger-review-token-TRANSFER-AND-DELETE.txt`
    (once the token is safely on Hostinger);
  - stop and remove the `masakhane-supervision-gateway-fourtypes-test` container (no test services
    on production);
  - let me know whether you opened SSH on port 22222.

## 2. Get everything into the repo

- Commit the gateway code (the Python container behind `/v1/supervision/*`) and the Caddyfile route
  to your branch. I can't review or redeploy what isn't in git, and the Caddyfile on the droplet is
  now a local edit that a deploy could conflict with.

## 3. Tier 3: write only to `qc_human_reviews`

- **I'll create the table** with an Alembic migration, with its rights and triggers. Please don't
  create anything in Postgres; `masakhane_qc_audit` (empty) will be dropped.
- **Columns:**

  | Column | Type | Notes |
  |---|---|---|
  | `id` | bigint, primary key | |
  | `recording_id` | bigint, references `recordings.id` | |
  | `decision` | text | `accept` or `reject` |
  | `reason_code` | text | Required when `decision = reject`; from the list below |
  | `note` | text, optional | Supervisor's comment; required when `reason_code = other` |
  | `reviewer_id` | text | The supervisor's ID in your dashboard |
  | `reviewer_name` | text | Display name, for reports |
  | `operation_key` | uuid, unique | Your idempotency key: a retried request is never saved twice |
  | `created_at` | timestamptz | Set by the database |
  | `notified_at` | timestamptz, empty | For contributor feedback later; leave it empty |

- **Decision values are `accept` and `reject`** (not `approve`, and deliberately not `accepted` /
  `rejected`, so they can't be confused with `qc_status`).
- **Rejection reasons (proposed):** `wrong_sentence`, `noise`, `not_own_voice`, `unclear`,
  `not_chichewa`, `other`. Please suggest additions from what supervisors actually hear before we
  fix the list.
- **The gateway only inserts rows:** one row per decision, never updated or deleted. A reversal is
  a new row; the latest row is the current verdict. The writer account will have `INSERT` and
  `SELECT` on this table only, and no rights on `recordings`.
- **Conflict check:** before saving, compare against the recording's current `qc_tier3` (not
  `qc_status`), so two supervisors can't overwrite each other without knowing.
- **Escalations, follow-ups and notes** stay in your dashboard's own tables. They aren't QC
  decisions.
- My triggers do the rest: a new row sets `recordings.qc_tier3` and `qc_tier3_reason`, and then
  `qc_final`.

## 4. What the dashboard reads

- New columns on `recordings`, filled by my code and triggers; read them, never write them:
  - `qc_tier2` (1–4) and its reason (`band_reason` in `qc_results`);
  - `qc_tier3`, `qc_tier3_reason`;
  - `qc_final`.
- Add them to the snapshot. I'll grant the read-only account access when the migration is in.
- **Two figures, labelled clearly:**
  - **Collected minutes** (contributor progress): the bot's rule, every clip not rejected by
    tier 1 (`qc_status <> 'rejected'`), so supervisors and contributors see the same number.
  - **Usable minutes** (dataset quality): `qc_final` in `gold` or `silver`; show `bronze` and
    `rejected` separately.

## 5. Before round 1: the sync

- Fetch only recordings changed since the last sync (`updated_at`), instead of the whole catalogue
  each time, and remove the 10,000-row limit. The main collection will pass 10,000 clips quickly.
  My triggers will update `updated_at` whenever a QC column changes, so changes are picked up.

## Timing

- Items 1 and 2: now.
- Items 3 and 4: prepare from this spec; nothing is enabled until my migration is in and we've tested
  it together (on a copy of the data, not production).
- Item 5: before round 1.

Can we have a call this week to go through it?
