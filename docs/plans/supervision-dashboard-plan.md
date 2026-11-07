# Supervision dashboard: plan

Owner: Stewart Chikomola. Reviewer: Dunstan Matekenya. Drafted 2026-11-07; decisions of
2026-11-07 included (sections 3, 4.4, 6 and 10).

## 1. Purpose

Supervisors need a web page where they log in, work through the recordings that the automatic
checks have flagged, listen to each one, and record a decision: **OK**, **reject** (with a
reason) or **escalate**. They also need to do **random spot checks** on recordings nobody flagged.
Every decision is saved in the app database with **who** made it and **when**, so the daily
report and the bot can use it: rejected clips stop counting towards the contributor's minutes, the
bot keeps giving them new tasks until they reach their target, and the contributor is told exactly
which clips were removed and why.

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
- Supervisors are the colleagues who manage contributors, so they **see participant names and
  phone numbers** to follow up. Because of that: personal accounts only (never shared), every
  audio playback, contact-detail view and action is logged, and the participant consent and
  privacy notice must say that supervisors may contact them. Dunstan and Stewart are admins
  (platform); supervisors do the review work.

## 4. Features

### 4.1 Login
- Username + password; passwords hashed (argon2 or bcrypt); signed, HTTP-only, secure session
  cookie; sessions expire (e.g. 12 hours); logout.
- Basic protection: rate-limit failed logins, CSRF protection on every form, HTTPS only (Caddy).

### 4.2 Overview (home page)
- Open items by reason (wrong sentence, ASR review, low speech, upload check, spot check) and by
  participant; items done today and by whom.
- Per participant: minutes recorded, minutes accepted, minutes pending review, minutes rejected.

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
- Decision buttons: **OK** (counts), **Reject** (doesn't count), **Escalate** (a person needs to
  follow up). Reject requires a reason code: wrong sentence, unclear, noisy, not Chichewa, cut
  off, different speaker, other. Escalate requires a note. Each reason code has a short
  contributor-facing explanation and tip (Chichewa and English) kept in config, used in the
  feedback messages (section 6).
- There is no "re-record" decision: contributors are not asked to record the same prompt again
  (section 6).
- A later decision overrides an earlier one (e.g. a supervisor reverses a rejection after
  following up); the history stays in the log.
- Contributor contact details (name, phone) shown on the review and participant pages.
- After saving: move to the next item. Keyboard shortcuts (space = play/pause, 1–3 = decisions)
  make long sessions faster.
- Must work on a phone (supervisors in the field) and on slow connections.

### 4.5 Spot checks
- "Spot check" draws a random recording that passed all checks and hasn't been reviewed, creates a
  `spot_check` item and opens it on the review page.
- Sampling should be fair: per participant and per task type. The rate is a **parameter** in
  `config/supervision.yaml` (e.g. `spot_check_rate: 0.05` of each participant's accepted clips per
  day), with a **daily cap** on spot-check items (`spot_check_max_per_day`) because supervisor
  time has a budget. The pilot values are not decided yet; the defaults must be easy to change
  without code changes.

### 4.6 Participant page
- Name and phone number; their recordings with status (accepted, pending, rejected,
  escalated), minutes per task type
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
(FK), `decision` (`ok`, `reject`, `escalate`), `reason_code`, `note`, `created_at`,
`notified_at` (when the contributor was told; set by the feedback job in Phase 5).

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

The dashboard only records decisions. Acting on them changes the bot, so it is a separate phase,
designed together. Decided on 2026-11-07:

- **Targets never change.** The per-participant target (e.g. 20 minutes) in
  `config/assignment_settings.yaml` stays fixed; nobody edits config after a review. What changes
  is the **counted minutes** towards that target, computed from the database:
  - clips rejected at arrival (too short, silent, forwarded) never count — as today;
  - clips rejected by a supervisor **stop counting when the contributor is notified** (the
    feedback digest sets `review_actions.notified_at`), so the bot's "you have X minutes left"
    always matches what the contributor has been told;
  - clips waiting for review keep counting until a decision is notified.
- **No re-recording of the same prompts.** A rejected clip simply stops counting:
  - **Contributor minutes:** the contributor is short of their target again, so the bot keeps
    giving them new tasks of that type until they reach it. That is the top-up.
  - **Prompt coverage:** coverage counts accepted, recorded and open assignments only, so a prompt
    whose clip is rejected goes back into the pool and is sent to another speaker.
  - **Spare prompts:** keep 20–30% more prompts than the targets need, so rejections never leave
    gaps (check with the simulation script).
- **Contributors are told exactly which clips were removed.** After review, the bot sends:
  1. a short summary ("5 clips from today were removed; you have 4 minutes left to record");
  2. one message per rejected clip, sent as a **WhatsApp reply to the contributor's own voice
     note** (Cloud API `context.message_id` = the voice note's message ID from
     `inbound_messages`), so their clip appears quoted above the reason and tip. Each message also
     names the clip in words ("Sentence: *Kodi Chikondi anamwa…*", "Picture: market scene,
     Saturday 3 pm") in case they deleted the original. Sent one after another, in order; capped
     (e.g. 10 per digest, the rest as a count).
  - One digest a day after review, not a message per decision. `review_actions.notified_at`
    prevents sending twice and shows supervisors whether the contributor was told.
- **WhatsApp's 24-hour rule:** the bot can send free-form messages only within 24 hours of the
  contributor's last message. Feedback is therefore delivered (a) at the start of their next
  session when they write to the bot, or (b) via a Meta-approved **template message** ("Your
  recordings have been reviewed. Reply OK to see the results."); once they reply, the window is
  open and the details follow. Templates are used only for reports.
- **Supervisors follow up by phone** as well, especially on escalations and repeated rejections.
- **Escalations are a supervisor's judgement, never automatic.** "Escalate" means "this needs a
  person to follow up" (e.g. the supervisor notices one contributor has many rejections, or the
  voice sounds like a different speaker). Escalated items appear in a follow-up list on the
  dashboard until a supervisor marks them handled, with a note of what was done. No automatic
  escalation rules and no notifications for now; the overview and participant pages show reject
  rates so supervisors can spot patterns themselves.
- The daily report includes supervision results: items reviewed, decisions by type,
  per-contributor reject rates, minutes removed.

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
| 5. Acting on decisions | Minute accounting, prompt coverage, feedback digest with quoted replies, template message (with Dunstan) | Bot behaviour changed and tested; feedback reaches a test contributor |
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

## 10. Decisions and open questions

Decided (2026-11-07):

1. Supervisors see contributor names and phone numbers (they manage and follow up contributors).
2. Decisions are OK / Reject (with reason code) / Escalate; no re-recording of the same prompts;
   rejected clips stop counting; spare prompts cover the gaps.
3. Contributors are told exactly which clips were removed, by WhatsApp replies quoting their voice
   notes; templates are used for reports when the 24-hour window has closed.
4. Targets in config never change after reviews; a supervisor-rejected clip stops counting towards
   the target when the contributor is notified.
5. Spot-check rate and daily cap are config parameters; pilot values to be set later (budget).
6. At most about 5 supervisors (names needed only when accounts are created at deploy).
7. Escalation is a manual supervisor decision shown in a follow-up list; no automatic escalation
   and no notifications for now.
8. Dashboard address (`/supervise` path or a subdomain): Stewart decides in Phase 6.

Still open:

9. Quoted replies to older voice notes: Stewart runs the test (`scripts/send_test_reply.py` on his
   own older recordings, with Dunstan) and records how far back WhatsApp shows the quote.
