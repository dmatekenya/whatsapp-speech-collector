# Chichewa Speech Collection via WhatsApp — Methodology and Design Decisions

Living document. Last updated: 2026-11-05. Covers the smartphone (SR) collection channel run through
the WhatsApp bot ("Tiyankhuleni"). Will feed the final methodology and QA documentation (work plan
Phase 5). Update it whenever a design decision changes; add a row to the decision log at the end.
Repo copy: `docs/methodology.md`.

Related project docs: `claude/todo.md` (tasks), `claude/architecture-database.md` (schema),
`claude/meta-cloud-api-migration-plan.md` (WhatsApp provider move). Prompt rules for annotators
live in the separate prompt creation guide.

## 1. Purpose and scope

- Goal: an open Chichewa speech dataset of about 500 hours for ASR (and a TTS-ready subset from the
  HQ group), recorded between Months 3 and 8 of the work plan (Sep 2026 – Aug 2027).
- **Speech mix:** about 30% scripted (read) speech and 70% spontaneous speech, following
  Masakhane guidance: roughly 150 hours read and 350 hours spontaneous. Current decision: picture
  descriptions, answers to conversation starters and unprompted free speech all count as
  spontaneous (unscripted, elicited speech); to be confirmed with Masakhane. Spontaneous speech
  needs transcription, while read speech mostly reuses the prompt text, so the transcription
  workload follows the spontaneous share. (The work plan's earlier figure of ~200 hours of read
  speech needs updating to match.)
- Two recording groups: HQ (20 speakers, USB/condenser microphones, recorded separately) and SR
  (smartphone cohorts, by district: 9 cohorts SR-C01–SR-C09, three districts each). This document
  covers the SR channel.

## 2. Participants

- **Recruitment and consent offline.** Participants are recruited and registered by the team
  before using the bot. At registration they are told about the project and the privacy notice and
  give consent. Participants must be 18 or older.
- **Registry.** Participants are loaded from batch CSV files (one file per batch). Each gets a
  pseudonymous ID (`P0001`…) assigned at import, never reused. Stored attributes: name, WhatsApp
  number, age, sex, district, first language, accent (configurable columns). Test participants are
  flagged `is_test` and excluded from exports.
- **Primary device.** The device the participant mainly records on is asked at registration and
  stored as optional attributes: `primary_device_type` (phone, tablet or computer),
  `primary_device_brand` (e.g. Tecno, Itel, Samsung, Apple) and `primary_device_model` (e.g.
  Spark 10, iPhone 12). This is the only source of device details: neither WhatsApp nor the Meta
  Cloud API says which device a voice note was recorded on. It is self-reported and describes the
  participant's usual device, not each recording.
- **Only registered numbers take part.** An unregistered number gets one reply saying so; none of
  its messages or recordings are kept.
- **Identity in the data.** Recordings are linked only to the participant ID; names and phone
  numbers stay in the restricted registry.

## 3. Collection channel

- **Why WhatsApp:** participants already use it daily (little training), and in Malawi people buy
  cheap WhatsApp-only data bundles, so recording costs them little. Everything stays inside
  WhatsApp (no links).
- **User-initiated only.** The bot only replies to participants' messages, so all messages fall in
  WhatsApp's free 24-hour service window; no paid template messages are used.
- **Provider.** Piloted with Twilio; moving to the Meta WhatsApp Cloud API direct (lower cost,
  voice-note and forwarded flags, no media clean-up). During the move both run side by side and
  replies always go out on the number the participant wrote to. From the pilot (starting
  2026-11-08), participants use the Meta number; the Twilio number remains only as a fallback until
  it is retired.
- **Comparison.** A browser-based recording app is being developed in parallel by a colleague;
  both channels will use the same prompt bank, IDs and QC so they can be compared.

## 4. Task types and prompts

Recordings are classified at two levels:

- **Speech type** (`speech_type`): *scripted*, read aloud from a given text, or *spontaneous*, in
  the participant's own words. This is the main split for ASR use (read vs. spontaneous speech).
- **Task type** (`task_type`): how the speech was elicited. Each task type belongs to exactly one
  speech type.

| Speech type | Task type | Shown to participants as | What the participant does | Prompted | Each prompt recorded by |
|---|---|---|---|---|---|
| Scripted | Read sentences (`sentence`) | Reading sentences | Reads a sentence aloud | Yes | 5 speakers |
| Spontaneous | Describe pictures (`image`) | Describing pictures | Describes an image in their own words | Yes | 7 speakers (pilot) |
| Spontaneous | Conversation starters (`conversation_starter`) | Answering questions | Answers an open question freely | Yes | 3 speakers |
| Spontaneous | Free speech (`free_speech`) | Talking about anything | Talks about anything they like ("speak from the heart") | No | — |

- Task types are defined in `config/assignment_settings.yaml` (`task_types`), each marked as
  prompted or not and given its speech type. The speech type is not stored per recording: it is
  derived from the task type, so changing a task type's speech type reclassifies all of its
  recordings. Reports (e.g. the task-bank simulation) show it next to the task type and sum
  minutes per speech type. With the current split the target is 30% scripted, 70% spontaneous.
- **Clip length:** 5–29 s for all tasks (29 s is one second below the 30 s window used by
  Whisper-style ASR models, leaving room for padding). Open question: whether longer uploaded
  free-speech files should be accepted and segmented later.
- **Prompt IDs** are assigned at import, one sequence per type: `S00001` sentences, `I0001` images,
  `C0001` conversation starters. Free speech has no prompts. Imports must run in the same order on
  every environment so IDs match (and match the browser app).
- **Sentence length is controlled at selection, not fixed in transcription.** The aim is that a
  read sentence fits within the clip limit, so the prompt text can serve as the transcript and
  read speech needs as little transcription as possible.
  - Import checks: target 8–25 words (hard limits 3–50); planned character-length check as well,
    since Chichewa words are long and characters predict reading time better than words. Out-of-
    target sentences are flagged, not rejected, and are assigned last.
  - After the pilot, limits are calibrated from real reading times (median duration and seconds
    per character per sentence); sentences with a median above ~27 s or below 5 s are retired.
  - Sensitive questionnaire topics are excluded.
- **Images:** organised into theme folders (theme = folder name); prepared copies are JPEG,
  metadata stripped, resized for WhatsApp. No text in images, no recognisable real people.
- **Conversation starters:** open questions that prompt free speech (not read aloud); in Chichewa
  for the pilot onwards.
- **Free speech (unprompted):** the bot invites the participant to talk about anything (their day,
  their village, a story); captures genuinely spontaneous speech. Needs transcription.
- **Coverage (prompted tasks):** a prompt stops being assigned once it has enough speakers
  (accepted + recorded + unexpired sent); least-covered prompts go first; nobody gets the same
  prompt twice.

## 5. Session design

- On contact the bot shows progress and today's task, sends instructions the first time per task
  type, and asks the participant to start (reply 1) or come back later (reply 2).
- A **session** is one task type, up to 10 clips, ending early when that type's target is met.
  The bot then offers the next type, or finishes when every type target is met.
- **Answer binding:** a voice note answers the open prompt; a swipe-reply binds it to the prompt
  replied to (same number only). To redo a recording, the participant swipe-replies to the same
  prompt and records again (deleting a voice note in WhatsApp is not reported to the bot); being
  verified in pre-pilot testing. In a free-speech session each clip is a numbered invitation
  ("Voice note 3 of 10: tell me about anything you like", with a rotating example topic), and every
  accepted voice note counts toward the free-speech target.
- **Unassigned recordings:** voice notes sent outside a session (before starting, or while asked
  whether to start) are kept but never counted toward any target. Only voice notes in a
  free-speech session count as free speech.
- **Retakes:** clips that are too short, too long, empty or without speech get a retake request (up
  to 2 retakes per prompt); failed attempts are stored as rejected. A forwarded voice note gets a request to
  record it themselves, but does not use up a retake: the prompt stays open.
- **Too-long sentences (planned):** after a first too-long reading the participant is asked to read
  a little faster; after a second, the sentence is skipped for that person. A sentence that runs
  too long for two different speakers is retired automatically for review.
- **Prompt expiry:** unanswered prompts expire after 24 hours and return to the pool.
- **Coming back:** within 24 hours the participant carries on (a reminder of the open prompt if
  they send text). After more than 24 hours (configurable), the session is paused, open prompts
  expire, and the participant gets the welcome-back summary. Exception: a voice note swiped onto a
  prompt that has not yet expired still counts as its answer.
- **Undelivered replies:** if a reply can't be delivered (outside WhatsApp's 24-hour window, or a
  provider error), the conversation does not move on: the participant's next message repeats the
  step. Recordings already received are kept.
- **Message wording** comes from a fixed message catalogue (English now, Chichewa planned) with
  random variants for captions and acknowledgements; the variant used is recorded.

## 6. Targets and collection runs

- **One fixed target per participant per run**, set as recorded minutes in the configuration
  (`per_participant`), with a fixed **split** across task types. Current split: 30% sentences, 25%
  pictures, 25% conversation starters, 20% free speech (20 min → 6 / 5 / 5 / 4), so 70% of the
  recorded minutes are spontaneous speech. Each type's target is rounded to the nearest 0.5 minute,
  and the total shown to participants is the sum of the type targets. The split must cover every
  task type and add up to 1, or the bot refuses to start. Rare individual exceptions are set per
  participant (a different total, or explicit minutes per type).
- **Effort:** participants spend roughly 3–4 times the recorded minutes (reading, retakes, waiting).
- **Pilot:** 15 participants × 20 recorded minutes over 3 days (≈20–25 minutes of effort a day).
- **Collection runs:** about 20 hours per weekly run (the pace needed for 500 hours over the
  recording months), e.g. 60 minutes per participant. Recruit about 20% extra to absorb drop-outs;
  any shortfall rolls into the next run.
- **Prompt supply:** run the task-bank simulation with the planned participants and content before
  each run; e.g. for the pilot, 24 images × 5 speakers ran out, so images use 7 speakers per
  prompt with ~50 images.

## 7. Audio handling

- Each voice note is stored as the original (`.ogg`) plus a 16 kHz mono WAV for QC and ASR, in a
  per-participant folder; files never move. Accepted and rejected sets are produced by export.
- Exact duplicates (re-sent audio) are detected by content hash.
- **Audio origin** (from the Meta Cloud API flags), stored per recording:
  - **recorded** (voice note recorded in WhatsApp): accepted as normal.
  - **forwarded** (from another chat): never accepted for any task; stored as rejected
    (`not_recorded`). In a session the participant is asked to record it themselves and the open
    prompt stays open (no retake used); outside a session the bot replies as usual. Forwarded wins
    over uploaded (a forwarded audio file counts as forwarded).
  - **uploaded** (audio file attached, e.g. recorded outside WhatsApp): accepted and counted, but
    flagged for review (`speaker_check_pending`) until a speaker check confirms it is the
    participant's voice.
  - **unknown** (Twilio-era recordings, which carry no flags): accepted as normal.
  - The rule for each origin is set in `config/assignment_settings.yaml` (`audio_origin`:
    accept, reject or accept_flagged). Existing recordings were backfilled from their flags.

## 8. Quality control

- **On arrival (worker):** duration bounds (5–29 s), empty audio, duplicates, audio origin,
  speech check; retake requests.
- **Speech check (energy-based):** the worker measures each voice note on its 16 kHz WAV and
  stores `active_seconds` (time in 30 ms frames louder than −45 dBFS) and `mean_dbfs` (overall
  level). After the duration check, for all task types:
  - under 1.5 s of active audio: rejected as `no_speech`; the participant is asked to hold the
    microphone button and speak clearly, and the same prompt is sent again. This uses up a retake,
    like a too-short clip.
  - otherwise, active audio under 20% of the clip: accepted and counted, but flagged for review
    (`needs_review`, `low_speech`). A clip already flagged for a speaker check keeps that flag.
  - Settings are in `config/assignment_settings.yaml` (`clip_checks`); `scripts/check_silence.py`
    shows the measurements and verdicts for a folder of WAVs, for calibration. Recordings stored
    before the check are measured by `scripts/backfill_speech_levels.py`, without changing their
    QC status. The energy check can't tell speech from other loud sound (traffic, music); voice
    activity detection and ASR come later.
  - **Calibration:** thresholds were set from pre-pilot test recordings (two speakers, two phones):
    deliberately silent clips had 0.0–0.5 s of active audio, while the shortest real speech clip of
    5 s or more had 2.4 s; listening confirmed both groups. WebRTC voice activity detection was
    also tried and rejected for this check, because it reported ~2 s of "speech" in a near-silent
    clip. Thresholds are to be recalibrated on pilot data.
- **Duration monitoring (planned, daily during collection):** a report of the clip-length
  distribution per task type (median, p10/p90, % under the minimum, % over the maximum); per
  sentence (median duration, seconds per character, outliers); and per participant (median
  duration, % too short or too long, retake rate, forwarded attempts). Participants who
  consistently rush, cut off or run long are followed up by a supervisor; automatic nudges from the
  bot may come later.
- **Planned fast QC (CPU):** clipping, voice activity, SNR estimate.
- **Planned daily batch job (CPU):** run over each day's recordings: speech/noise detection with a
  stronger voice activity model (e.g. Silero VAD) to catch loud non-speech clips; speaker
  consistency checks (uploaded clips first); and the duration report. Results feed supervisor
  follow-up and the `needs_review` queue.
- **Batch ASR (GPU, twice a day in the pilot):** two steps.
  - **Step 1, transcription (in place):** recordings are sent to the fine-tuned Chichewa Whisper
    model (`whisper_b`, `dmatekenya/whisper-large-v3-chichewa-variant-b-normalized-transcript`;
    `whisper_v1` available) on a Modal GPU. Inference is the same as in the model's training
    evaluation: the `openai/whisper-large-v3` processor, language token "shona" (Whisper has no
    Chichewa token), task "transcribe", no forced decoder IDs, attention mask, fp16, greedy
    decoding. Sent: stored recordings of registered participants, except clips rejected at arrival
    (empty, too short or long, `no_speech`, forwarded), duplicates, test participants, and ones the
    model has already transcribed. Stored per clip in `transcripts`: the raw text (not normalised),
    the average token log-probability, and the model's repo and commit, so every transcript can be
    traced to the exact model. Each run is recorded in `asr_batches`; audio is passed in the call
    and never stored on Modal. Runs are scheduled in `config/asr_batch.yaml` (`runs_per_day`, from
    02:00 Malawi time) and can be run on demand.
  - **Step 2, scoring (next):** compare read speech against the prompt (CER/WER) to accept, send
    to human review, or reject.
- **Planned speaker check:** compare uploaded clips (and later all clips) with a voice profile
  built from the participant's own recorded voice notes (speaker embeddings); also near-duplicate
  checks. Human review by supervisors.
- QC thresholds to be calibrated on pilot data.

## 9. Privacy and data protection

- Privacy notice for participants (draft) covers what is collected, purpose, dataset use, sharing,
  retention, security and participants' choices (stop at any time; deletion on request).
- Phone numbers and names are restricted; logs never show full phone numbers, message text, media
  URLs or WhatsApp message IDs.
- Secrets live only in environment files on the server; never committed.
- Open: dataset licence (suggested CC BY 4.0, subject to grant terms), retention period for
  contact details, final hosting region.

## 10. Decision log

| Date | Decision |
|---|---|
| 2026-10 | WhatsApp as the SR channel; user-initiated sessions to avoid template costs |
| 2026-10 | Offline registration via batch CSVs; pseudonymous participant IDs |
| 2026-10 | Three task types (sentences, pictures, conversation starters); audio stimuli dropped for the pilot |
| 2026-10 | Speakers per prompt: 5 sentences, 5 images, 3 conversation starters; clips 5–30 s |
| 2026-10 | Sessions of up to 10 prompts per task type, ending early at the type target |
| 2026-10 | Audio kept as original + 16 kHz WAV; files never move; content-hash duplicate detection |
| 2026-10 | QC in two stages: fast checks in the worker, ASR faithfulness in GPU batches |
| 2026-10-30 | Deployed on a DigitalOcean droplet (Docker Compose, HTTPS) |
| 2026-11-03 | Move from Twilio to the Meta WhatsApp Cloud API; replies go out on the number the participant wrote to |
| 2026-11-05 | Targets: one `per_participant` number per run plus a fixed split; total computed |
| 2026-11-05 | Collection runs of ~20 h/week, ~20% extra recruitment, shortfalls roll over |
| 2026-11-05 | Pilot: 15 participants × 20 recorded minutes over 3 days |
| 2026-11-05 | Fresh start after 24 h without contact; 24-hour window guard on all outgoing messages |
| 2026-11-05 | Participants registered offline with consent; only registered numbers take part |
| 2026-11-05 | Maximum clip length 29 s (was 30); sentence length controlled at prompt selection so read speech needs minimal transcription; too-long sentences skipped and auto-retired; daily duration monitoring per type, sentence and participant |
| 2026-11-05 | Images: 7 speakers per prompt for the pilot (simulation showed 24 × 5 runs out) |
| 2026-11-05 | New unprompted task type "free speech"; split 30/25/25/20 (sentences/pictures/conversation/free speech), i.e. 70% spontaneous speech, following Masakhane guidance (~150 h read, ~350 h spontaneous of 500 h); pictures, conversation starters and free speech count as spontaneous (to confirm with Masakhane) |
| 2026-11-05 | Audio origin: forwarded audio never accepted; uploaded audio accepted but flagged for a speaker check; recorded voice notes accepted |
| 2026-11-05 | Free speech is run like the prompted types: each invitation is an assignment without a prompt, so session size, expiry, retakes, early end and progress work the same |
| 2026-11-05 | Voice notes outside a session are kept as "unassigned recordings" and never counted; only free-speech sessions count toward the free-speech target |
| 2026-11-05 | Forwarded voice notes never use up a retake (the prompt stays open); outside a session they are stored as rejected and the bot replies as usual |
| 2026-11-05 | Participant-facing task names and progress labels live in the message catalogue (per language), not the configuration |
| 2026-11-05 | Two-level classification: each task type has a speech type, scripted (sentences) or spontaneous (pictures, conversation starters, free speech); derived from the task type, not stored per recording; reports sum minutes per speech type (target 30% scripted, 70% spontaneous) |
| 2026-11-05 | Primary recording device (type phone/tablet/computer, brand, model) collected at registration as optional participant attributes, since WhatsApp and Meta don't report the device |
| 2026-11-05 | Pilot runs on the Meta Cloud API number (+1 618); the Twilio number stays only as a fallback until retired |
| 2026-11-05 | Speech check on arrival (energy-based, 30 ms frames above −45 dBFS): under 1.5 s of active audio rejected as `no_speech` with a retake request (uses a retake); under 20% active accepted but flagged `low_speech`; `active_seconds` and `mean_dbfs` stored per recording, older recordings backfilled without changing their QC status; thresholds to be calibrated on pilot data |
| 2026-11-05 | Pre-pilot testing by a tester: silent voice notes were being accepted (fixed by the speech check); uploaded audio accepted by design (flagged for a speaker check, to be run in a daily batch job); redo by swipe-replying to the prompt, since WhatsApp deletions aren't reported to the bot |
| 2026-11-06 | ASR batch step 1: transcription on a deployed Modal GPU app (Whisper `whisper_b`, training-time inference settings), called from the droplet twice a day; raw transcripts, average log-probability and the model's commit stored per recording and model (history kept); arrival rejections, duplicates and test participants not sent; scoring against prompts is step 2 |
