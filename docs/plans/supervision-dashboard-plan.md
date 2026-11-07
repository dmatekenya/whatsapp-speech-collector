# Supervision dashboard: plan

Owner: Stewart Chikomola. Reviewer: Dunstan Matekenya. Drafted 2026-11-07.

## 1. Purpose

Supervisors need a web page where they log in, work through the recordings that the automatic
checks have flagged, listen to each one, and record a decision: **OK**, **re-record**, **reject**
or **escalate**. They also need to do **random spot checks** on recordings nobody flagged. Every
decision is saved in the app database with **who** made it and **when**, so the daily report and
the bot can use it: for example, the next day the system can see that participant X has three
clips marked for re-recording, and either ask for them again or stop counting those minutes
towards X's target.

## 2. Where it fits in the system

What already exists:

- **Recordings:** every voice note is a row in `recordings` (participant, `wav_path` and
  `original_path` relative to `AUDIO_ROOT`, duration, `audio_origin`, arrival-check result in
  `qc_status` / `qc_reason`). Sentences, pictures and questions link to `assignments`, which links
  to `prompts` (sentence text, question text, or image). Free speech has no assignment.
- **Arrival checks** (worker, on every clip): too short / too long, forwarded (`not_recorded`) and
  silent (`no_speech`) are rejected; `low_speech` and uploaded clips (`speaker_check_pending`) are
  marked `needs_review`. **These `needs_review` clips are the first things supervisors should see.**
- **ASR transcription** (step 1, live since 2026-11-06): twice a day the droplet sends new clips to
  a Modal GPU and stores transcripts in `transcripts` (one row per recording per model).
- **ASR scoring and report** (step 2, to be built by Dunstan): compares sentence transcripts with
  the prompt (CER), sets a suggested band (accept / review / likely wrong sentence) and produces
  the daily report. **Its "review" and "wrong sentence" suggestions are the second source of items
  for supervisors.**

The dashboard sits on top: it **reads** recordings, prompts, transcripts and check results, and
**writes** only supervision tables (section 5). It never deletes or moves audio.

```
arrival checks ─┐
ASR report ─────┼──► review_items (what needs a human)  ──►  dashboard  ──► review_actions (who, what, when)
spot checks ────┘                                                         └► recording's supervision status
                                                                              (read by report and bot)
```

## 3. Users and access

- **Supervisor:** logs in, works the review queue, does spot checks, sees participant summaries.
- **Admin (Dunstan, Stewart):** everything a supervisor can do, plus manage supervisor accounts
  and see the full action log.
- Accounts are created by an admin (CLI script first; an admin page later). No self sign-up.
- Supervisors see **participant IDs** (P0012), not names or phone numbers, unless Dunstan decides
  otherwise (open question 1). Every audio playback and every action is logged.

## 4. Features

### 4.1 Login
- Username + password; passwords hashed (argon2 or bcrypt); signed, HTTP-only, secure session
  cookie; sessions expire (e.g. 12 hours); logout.
- Basic protection: rate-limit failed logins, CSRF protection on every form, HTTPS only (Caddy).

### 4.2 Overview (home page)
- Open items by reason (wrong sentence, ASR review, low speech, upload check, spot check) and by
  participant; items done today and by whom.
- Per participant: minutes recorded, minutes accepted, minutes pending review, minutes marked for
  re-record or rejected.

### 4.3 Review queue
- List of open items, filterable by participant, reason, task type and date; sorted by priority
  then age.
- "Start reviewing" opens the next item; the item is **claimed** by that supervisor for a short
  time (e.g. 10 minutes) so two people don't review the same clip at once.

### 4.4 Review page (the core screen)
- Audio player (seekable), with play count.
- What was asked: sentence or question text, or the image (thumbnail from the prepared images);
  "free speech" if none.
- What the model heard: latest ASR transcript, CER and suggested band (once step 2 exists).
- Facts: participant, task type, duration, speech seconds, audio origin, arrival-check result,
  time received, earlier actions on this clip.
- Decision buttons: **OK**, **Re-record**, **Reject**, **Escalate**; optional note (required for
  Reject and Escalate); reason codes (wrong sentence, unclear, noisy, not Chichewa, cut off,
  different speaker, other).
- After saving: move to the next item. Keyboard shortcuts (space = play/pause, 1–4 = decisions)
  make long sessions faster.
- Must work on a phone (supervisors in the field) and on slow connections.

### 4.5 Spot checks
- "Spot check" draws a random recording that passed all checks and hasn't been reviewed, creates a
  `spot_check` item and opens it on the review page.
- Sampling should be fair: e.g. per participant and per task type, with a configurable target
  (e.g. 5% of each participant's accepted clips). Settings in `config/supervision.yaml`.

### 4.6 Participant page
- Their recordings with status (accepted, pending, re-record, rejected), minutes per task type
  against target, and all supervision actions on their clips.

### 4.7 Action log (admin)
- Every action, filterable by supervisor and date; CSV export.

## 5. Data model (new tables, one Alembic migration per phase)

Names are a proposal; check against the current models before writing the migration.

**`supervisors`**: `id`, `username` (unique), `display_name`, `password_hash`, `role`
(`supervisor` / `admin`), `active`, `created_at`, `last_login_at`.

**`review_items`** (what needs a human; one open item per recording per reason):
`id`, `recording_id` (FK), `source` (`arrival_check`, `asr_report`, `spot_check`, `manual`),
`reason` (`low_speech`, `speaker_check_pending`, `asr_review`, `asr_wrong_sentence`,
`spot_check`, …), `priority`, `details` (JSONB: e.g. CER, band, batch ID), `status` (`open`,
`claimed`, `done`, `cancelled`), `claimed_by` / `claimed_until`, `created_at`, `resolved_at`.

**`review_actions`** (append-only audit trail; never updated or deleted):
`id`, `review_item_id` (FK, nullable for ad-hoc actions), `recording_id` (FK), `supervisor_id`
(FK), `decision` (`ok`, `rerecord`, `reject`, `escalate`), `reason_code`, `note`, `created_at`.

**Recording's supervision status:** the latest decision per recording, as a view
(`recording_supervision_status`) rather than a column, so it can't drift from the action log.
The report and the bot read this view.

**Audio access log:** `audio_access_log` (`supervisor_id`, `recording_id`, `accessed_at`), or the
same in the application log.

Who writes what:

| Table | Written by |
|---|---|
| `review_items` | arrival-check backfill and worker hook (`needs_review` clips); ASR step 2 (review / wrong-sentence bands); spot-check sampler; admin (manual) |
| `review_actions` | dashboard only |
| `supervisors` | admin script / admin page |

Step 2 will be written to create `review_items` in this format, so the schema in Phase 1 is the
contract between Stewart's and Dunstan's work. Agree it before either side builds on it.

## 6. What happens after a decision (Phase 5, with Dunstan)

The dashboard only records decisions. Acting on them changes the bot and the target accounting, so
it is a separate phase, designed together:

- **Counting minutes:** rejected and re-record clips stop counting towards the participant's
  targets; the bot then naturally asks for more of that task type.
- **Re-record requests:** the bot sends the participant the same prompt again with a short
  explanation (within WhatsApp's 24-hour window, or at their next session).
- **Escalations:** listed for an admin; e.g. a participant who keeps reading the wrong sentence
  is contacted by their supervisor.
- The daily report includes supervision results: items reviewed, decisions by type, per-participant
  re-record and reject rates.

## 7. Technical approach (recommended)

- **Same repository and stack:** Python, FastAPI, SQLAlchemy models, Alembic migrations, Postgres.
- **Server-rendered pages** with Jinja2 templates and **htmx** for small interactive parts (claim
  next item, save a decision without reloading). No separate JavaScript build. Plain, light CSS.
- **Separate service, same image:** a `dashboard` service in `docker-compose.yml` running the
  dashboard app, so dashboard problems can't affect the webhook that receives voice notes.
- **Address:** `https://bot.zazoai.com/supervise` (Caddy route) or a subdomain such as
  `supervise.zazoai.com` (needs a DNS record). Decide in Phase 6.
- **Audio:** streamed through an authenticated endpoint by recording ID (never by file path), with
  HTTP Range support so the player can seek. Serve the original `.ogg` (small, good on mobile
  data) with the `.wav` as a fallback for browsers that can't play Ogg/Opus (older iPhones).
- **Settings** in `config/supervision.yaml` (spot-check rate, claim time, session length,
  priorities per reason); secrets (session key) in `.env`.

## 8. Phases and deliverables

Each phase is one pull request into `main`, reviewed by Dunstan.

| Phase | Deliverable | Done when |
|---|---|---|
| 0. Set-up | Branch, local environment, seed script with **synthetic** data (fake participants, your own test recordings) | Dashboard runs locally with seed data; no real participant data on the laptop |
| 1. Data and login | Migration for `supervisors`, `review_items`, `review_actions` + status view; `scripts/create_supervisor.py`; login/logout; dashboard service skeleton | Tests pass; schema agreed with Dunstan |
| 2. Review queue and page | Queue, claim, review page, audio streaming, decisions; backfill `review_items` from existing `needs_review` clips and a worker hook for new ones | A supervisor can review a flagged clip end to end on phone and laptop |
| 3. Spot checks | Sampler + button + config | Spot-check items created fairly and reviewed like any other item |
| 4. Overview, participant page, action log | Summaries, filters, CSV export | Numbers match direct SQL queries |
| 5. Acting on decisions | Minute accounting and re-record requests (with Dunstan) | Bot behaviour changed and tested |
| 6. Deploy | Compose service, Caddy route, `.env` settings, `docs/deploy.md` section | Running on the droplet behind HTTPS; first supervisor accounts created |

## 9. Ways of working

- **Branches:** `feature/supervision-dashboard` for the overall work, or (better) one short-lived
  branch per phase (`supervision/phase-1-data-login`, …) merged by pull request. Keep up to date
  with `main` (`git pull origin main` into your branch) at least daily, because Dunstan is
  working on the same code.
- **Migrations:** create them with Alembic on top of the latest `main`. If both branches add a
  migration, they will need merging into one chain before deploy; tell Dunstan when you add one.
- **Claude Code:** fine to use; it follows `CLAUDE.md` (never touch `.env`, never commit `data/`,
  never log phone numbers). Review what it writes before committing.
- **Tests:** pytest for every endpoint and for permissions (a supervisor can't use admin pages; a
  logged-out user can't reach audio). Run `uv run pytest -q` before each pull request.
- **Data rules:** develop with synthetic data. Real participant audio stays on the droplet. Don't
  copy the production database or audio to your laptop.
- **Deploys:** only from `main`, after review, following `docs/deploy.md` (backup first). Switch
  to the app user with `sudo -iu dun` on the droplet.

## 10. Open questions for Dunstan

1. Do supervisors see participant names and/or phone numbers, or only IDs?
2. Final list of decisions and reason codes (is "reject" different from "re-record" for the bot?).
3. Who are the supervisors for the pilot, and how many accounts?
4. Spot-check rate for the pilot (e.g. 5% per participant per day)?
5. Should a rejected clip stop counting towards targets immediately, or only after the daily report?
6. Address: `/supervise` path or a subdomain?
7. Does an escalation need a notification (email/WhatsApp to an admin), or is a list on the
   dashboard enough for now?
