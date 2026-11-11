# Chichewa Speech Collection via WhatsApp — Methodology and Design Decisions

Living document. Last updated: 2026-11-10. Covers the smartphone (SR) collection channel run through
the WhatsApp bot ("Tiyankhuleni"). Will feed the final methodology and QA documentation (work plan
Phase 5). Update it whenever a design decision changes; add a row to the decision log at the end.
Repo copy: `docs/methodology.md`.

Related project docs: `claude/todo.md` (tasks), `claude/architecture-database.md` (schema),
`claude/meta-cloud-api-migration-plan.md` (WhatsApp provider move). Prompt rules for annotators
live in the separate prompt creation guide.

## Contents

- [Overview](#overview)
- [1. Purpose and scope](#1-purpose-and-scope)
- [2. Participants](#2-participants)
- [3. Collection channel](#3-collection-channel)
- [4. Task types and prompts](#4-task-types-and-prompts)
- [5. Session design](#5-session-design)
- [6. Targets and collection runs](#6-targets-and-collection-runs)
- [7. Audio handling](#7-audio-handling)
- [8. Quality control](#8-quality-control)
  - [8.1 Overview of the QC protocol](#81-overview-of-the-qc-protocol)
  - [8.2 Tier 1: arrival checks](#82-tier-1-arrival-checks)
  - [8.3 Tier 2: ASR batch checks](#83-tier-2-asr-batch-checks)
  - [8.4 Tier 3: human review](#84-tier-3-human-review)
  - [8.5 Final label](#85-final-label)
  - [8.6 Planned checks](#86-planned-checks)
- [9. Privacy and data protection](#9-privacy-and-data-protection)
- [10. Decision log](#10-decision-log)

## Overview

- **What:** an open Chichewa speech dataset (~500 hours) for speech recognition, about 30% read
  sentences and 70% spontaneous speech, recorded by adult volunteers on their own phones.
- **How:** contributors are registered offline with consent, then record voice notes in WhatsApp
  with a bot ("Tiyankhuleni"). The bot sends tasks (read a sentence, describe a picture, answer a
  question, or talk freely), checks each voice note on arrival and tracks progress toward each
  contributor's target in minutes.
- **Where:** the bot, database and audio run on one server (DigitalOcean droplet) and use the Meta
  WhatsApp Cloud API. A fine-tuned Chichewa Whisper model on a rented GPU (Modal) transcribes
  recordings twice a day. Supervisors review clips in a web dashboard.
- **Quality:** every clip passes through up to three tiers of checks (instant checks on arrival,
  automatic checks after transcription, human review) that combine into one label per clip:
  Bronze (not yet validated), Silver (validated automatically), Gold (validated by a human) or
  Rejected (section 8).
- **Privacy:** recordings are linked only to a pseudonymous participant ID; names and phone numbers
  stay in a restricted registry (section 9).
- **Status:** pilot from 2026-11-09 (16 contributors × 20 minutes); main collection in weekly runs
  afterwards.

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
- **Registration welcome.** After import, each new participant is sent a WhatsApp welcome (a
  utility template, `registration_welcome`) with their first name and total target in minutes, and
  is asked to save the number and reply Hi. It is sent once per participant (only resent on
  request), and not to participants who have already written to the bot.
- **Only registered numbers take part.** An unregistered number gets one reply saying so; none of
  its messages or recordings are kept.
- **Identity in the data.** Recordings are linked only to the participant ID; names and phone
  numbers stay in the restricted registry.

## 3. Collection channel

- **Why WhatsApp:** participants already use it daily (little training), and in Malawi people buy
  cheap WhatsApp-only data bundles, so recording costs them little. Everything stays inside
  WhatsApp (no links).
- **User-initiated, plus one welcome.** The bot only replies to participants' messages, so all
  conversation messages fall in WhatsApp's free 24-hour service window. The one exception is the
  registration welcome (section 2): participants haven't written yet, so it is an approved utility
  template, which WhatsApp allows outside the window and charges for (under about US$0.01 per
  message). The 24-hour check still applies to every other message.
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
- **Clip length:** participants are asked for about 15–30 s. Limits are set per task type so slow
  readers and natural speech aren't penalised; long clips are kept and split into shorter pieces
  during preprocessing, before training (not at collection time):

  | Task type | Minimum | Flagged as long (kept, counted) | Rejected above |
  |---|---|---|---|
  | Sentences | 5 s | over 30 s: `long_sentence` | 45 s |
  | Pictures, conversation starters | 5 s | over 30 s: `long_clip` | 60 s |
  | Free speech, recorded in WhatsApp | 5 s | over 30 s: `long_clip` | 60 s |
  | Free speech, forwarded or uploaded | 5 s | over 30 s: `long_clip` | 120 s |
  | Outside a session | never rejected for length | over 30 s: `long_clip` | — |

  Clips between the minimum and the rejection limit are accepted and counted in full (subject to
  the other checks: audio origin, speech check, duplicates). The long flag
  (`recordings.length_flag`) is information for preprocessing, not a review flag: it never puts a
  clip into `needs_review`. Too short or too long: a retake request naming the limit for that task
  ("keep it under 45 seconds" / "1 minute" / "2 minutes"). Limits are in
  `config/assignment_settings.yaml` (`clip_limits`). 30 s is Whisper's window: clips over 30 s
  need long-form ASR (section 8).
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
    per character per sentence); sentences whose readings are usually flagged `long_sentence`
    (median over 30 s) or below 5 s are retired. This is a later, report-driven step.
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
- **Retakes:** clips that are too short, too long (limits per task type, section 4), empty or
  without speech get a retake request (up to 2 retakes per prompt); failed attempts are stored as
  rejected. Outside free speech, a forwarded
  voice note or an attached audio file gets a request to record a new voice note, but does not use
  up a retake: the prompt stays open. In free speech both are accepted (see section 7), so the
  usual retakes apply.
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
  - **forwarded** (from another chat, e.g. an older voice note the participant once sent a friend)
    and **uploaded** (an audio file attached, e.g. recorded outside WhatsApp) follow the same rule:
    - **in free speech:** accepted and counted toward the free-speech target, but flagged for review
      (`needs_review`, `speaker_check_pending`) until a speaker check confirms it is the
      participant's own voice. Older recordings are often more natural speech. The usual checks
      still apply (5 s to 2 minutes, speech check, duplicates, retakes), and the bot replies as for any
      accepted clip. `audio_origin` stays `forwarded` or `uploaded`, so these clips can always be
      told apart.
    - **in all other task types** (sentences, pictures, conversation starters): not accepted;
      stored as rejected (`not_recorded`). The participant is asked to record a new voice note (the
      reply says whether it was forwarded or an audio file) and the open prompt stays open (no
      retake used).
    - **outside a session:** stored as rejected (`not_recorded`) and the bot replies as usual.
    - Forwarded wins over uploaded (a forwarded audio file counts as forwarded).
  - The bot's instructions are kept short, since contributors get a full guide. The free-speech
    instructions say an older voice note or audio file of their own (up to 2 minutes) may be sent
    if only their voice is in it. The other task types' instructions don't mention forwarding;
    the reply to a forwarded or uploaded clip asks for a new recording.
  - **unknown** (Twilio-era recordings, which carry no flags): accepted as normal.
  - The rule for each origin is set in `config/assignment_settings.yaml` (`audio_origin`:
    accept, reject or accept_flagged), and a task type can override it
    (`task_types.free_speech.audio_origin`: forwarded and uploaded accept_flagged). Existing
    recordings were backfilled from their flags; recordings from before these changes keep their
    status (uploads accepted earlier in other task types stay `speaker_check_pending`).

## 8. Quality control

### 8.1 Overview of the QC protocol

Each clip passes through up to three tiers of checks. Each tier records its own result in its own
column, and a fourth column combines them into one final label. No tier changes another tier's
column.

| Tier | When | Who | Recorded in | Values |
|---|---|---|---|---|
| 1. Arrival checks | on arrival | the bot | `recordings.qc_status`, `qc_reason` | `pending`, `accepted`, `needs_review`, `rejected` |
| 2. ASR batch checks | after each transcription batch (twice a day in the pilot) | scoring on the server | `qc_results` (history), `recordings.qc_tier2` | 1 (probably unusable) to 4 (all signals good); empty = not scored |
| 3. Human review | flagged clips and random spot checks | supervisors, in the dashboard | `qc_human_reviews` (history), `recordings.qc_tier3`, `qc_tier3_reason` | `accept`, `reject`; empty = no human decision |
| Final label | whenever a tier changes | database triggers | `recordings.qc_final` | `bronze`, `silver`, `gold`, `rejected` |

- **Only tier 1 and humans reject.** Tier 2 never rejects a clip on its own; low levels send it to
  a supervisor.
- **Tier 1 is never overwritten.** `qc_status` drives the bot's retakes and progress counts, so
  later tiers write only their own columns.
- **Rules live in one place:** the final label is set by database triggers from the tier columns
  (section 8.5), not by the bot or the dashboard.
- **Labels follow the data-lake convention of increasing trust:** Bronze → Silver → Gold. Here Gold
  means checked by a human, not aggregated as in a data warehouse.

### 8.2 Tier 1: arrival checks

- **On arrival (worker and conversation engine):** the worker measures duration and speech levels
  on the WAV and applies the default audio-origin rule and long flag; once the clip is matched to
  a prompt, the engine applies the task type's rules: audio origin, empty audio, duplicates,
  duration limits (section 4), speech check; retake requests.
- **Speech check (energy-based):** the worker measures each voice note on its 16 kHz WAV and
  stores `active_seconds` (time in 30 ms frames louder than −45 dBFS) and `mean_dbfs` (overall
  level). After the duration check, for all task types:
  - under 1.5 s of active audio: rejected as `no_speech`; the participant is asked to hold the
    microphone button and speak clearly, and the same prompt is sent again. This uses up a retake,
    like a too-short clip.
  - a clip over 30 s with active audio under 20% of it: also rejected as `no_speech` (same reply,
    uses a retake). Without this a long, mostly silent clip would count in full toward the target
    (e.g. 2 s of speech in a 2-minute clip).
  - otherwise, active audio under 20% of the clip: accepted and counted, but flagged for review
    (`needs_review`, `low_speech`). A clip already flagged for a speaker check keeps that flag.
    The long flag is separate and doesn't affect this.
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
### 8.3 Tier 2: ASR batch checks

- **Batch ASR (GPU, twice a day in the pilot):** two steps.
  - **Step 1, transcription (in place):** recordings are sent to the fine-tuned Chichewa Whisper
    model (`whisper_b`, `dmatekenya/whisper-large-v3-chichewa-variant-b-normalized-transcript`;
    `whisper_v1` available) on a Modal GPU. Inference is the same as in the model's training
    evaluation: the `openai/whisper-large-v3` processor, language token "shona" (Whisper has no
    Chichewa token), task "transcribe", no forced decoder IDs, attention mask, fp16, greedy
    decoding. Sent: stored recordings of registered participants, except clips rejected at arrival
    (empty, too short or long, `no_speech`, forwarded or uploaded outside free speech), duplicates, test participants, and ones the
    model has already transcribed. Stored per clip in `transcripts`: the raw text (not normalised),
    the average token log-probability, and the model's repo and commit, so every transcript can be
    traced to the exact model. Each run is recorded in `asr_batches`; audio is passed in the call
    and never stored on Modal. Runs are scheduled in `config/asr_batch.yaml` (`runs_per_day`, from
    02:00 Malawi time) and can be run on demand.
  - **Clips over 30 s:** Whisper decodes 30 s at a time, so longer clips use Whisper's long-form
    generation, with the same settings:
    - The clip is decoded window by window, without being cut at 30 s, and each clip is decoded on
      its own.
    - The text of each segment is decoded separately and the segments are joined with a space.
      Decoding them together ran words together at segment boundaries.
    - These transcripts have no average log-probability, and `transcripts.long_form` records
      which clips used long-form.
    - Tested on real 60–120 s clips with both models before it was enabled.
    - A result for a long clip from an app without long-form (i.e. not yet redeployed) is not
      stored, so the clip is retried.
  - **Step 2, scoring (being built):** measures every transcribed clip on the droplet's CPU, with
    no GPU. A deliberately short set of metrics: each one catches a specific problem that can be
    confirmed by listening to the clip.

    **All recordings: audio**

    | Metric | Unit | What it catches | Stored in |
    |---|---|---|---|
    | `duration_s` | seconds | too short or too long (enforced on arrival) | `recordings` (exists) |
    | speech ratio | 0–1 | mostly silence, long pauses, recorder left running | computed from `recordings.active_seconds` ÷ `duration_s` (both exist) |
    | `mean_dbfs` | dBFS | speaker too quiet or far from the phone | `recordings` (exists; overall level, including pauses) |
    | `snr_db` | dB | background noise (traffic, market, TV, wind) | new column on `recordings`: WADA-SNR estimate |

    **All recordings: transcript** (from the latest `whisper_b` transcript)

    | Metric | Unit | What it catches | Stored in |
    |---|---|---|---|
    | `word_count` | words | empty or near-empty answers | `qc_results` |
    | `words_per_second` | words ÷ `active_seconds` | very slow: hesitant or partly silent; very fast: often text the model invented | `qc_results` |
    | `avg_logprob` | log-probability (closer to 0 = surer) | model unsure: noise, mumbling, another language | `transcripts` (exists; empty for long-form clips over 30 s) |
    | `compression_ratio` | ratio, ~1–2 normal | repeated phrases from model hallucination (Whisper treats > 2.4 as suspect) | `qc_results` |

    **Sentences only** (transcript vs the prompt text sent, `assignments.prompt_text_snapshot`;
    both normalised with the training normaliser)

    | Metric | Unit | What it catches | Stored in |
    |---|---|---|---|
    | `cer` | 0–1+ | the main measure of whether the right sentence was read | `qc_results` |
    | `wer` | 0–1+ | reported alongside CER, not used for decisions | `qc_results` |
    | `length_ratio` | transcript ÷ prompt characters | explains a high CER: well below 1 = stopped early; above 1 = extra speech | `qc_results` |
    | `has_digits` | true/false | prompt contains numbers: `whisper_b` writes numbers as words, so CER is unreliable; such clips go no further than review | `qc_results` |

    - **Where results live:** facts about the audio itself, which never change, stay on
      `recordings` next to the arrival measurements (new: `snr_db`). Everything that depends on a
      transcript, a model or the normaliser goes into a new table, `qc_results`: one row per
      scoring run per recording, with one column per metric (`word_count`, `words_per_second`,
      `compression_ratio`; for sentences `cer`, `wer`, `length_ratio`, `has_digits`), plus
      `recording_id`, `transcript_id`, `stage` (`asr`), `band`, `band_reason`, `metrics_version`,
      `thresholds_version` and `created_at`. Inputs that already exist are not copied
      (`duration_s`, `active_seconds`, `mean_dbfs`, `snr_db` on `recordings`; `avg_logprob` in
      `transcripts`).
    - **Which clips:** stored clips not rejected on arrival, with a `whisper_b` transcript, that
      answer a prompt or free-speech invitation. Unassigned recordings (sent outside a session)
      are never counted and are not scored (`qc_tier2` stays empty); nor are test participants'
      clips, unless asked for.
    - **Order:** `snr_db` for clips without it; then, per clip with a new transcript, the metrics,
      the band from the rules, a new `qc_results` row, and in the same transaction the band copied
      to `recordings.qc_tier2`. Re-scoring after a cut-off change repeats the last steps with a new
      `thresholds_version` and adds rows rather than overwriting, so versions can be compared;
      `qc_tier2` always shows the latest.
    - **Not measured:** clipping (rare after WhatsApp's own audio processing) and a separate
      speech-only level (the speech ratio already covers pauses). Either can be added later if
      calibration shows a need.
    - **Limitations:** WhatsApp's noise suppression can make SNR look better than the room was, so
      SNR is used to rank clips and its cut-offs are calibrated on our data. `active_seconds` is
      energy-based and counts any loud sound, not only speech.
    - **Tier-2 level (`qc_tier2`):** a whole number from 1 to 4 (empty = not scored yet), stored
      as `qc_results.band` and copied to `recordings.qc_tier2`. Each level leads to a different
      action; the values deliberately differ from `qc_status` so the two can't be confused.

      | Level | Meaning | Action |
      |---|---|---|
      | 4 | All signals good | Usable as is (Silver) |
      | 3 | Acceptable, one borderline signal | Usable (Silver), spot-checked more often |
      | 2 | A clear concern | Review queue (stays Bronze) |
      | 1 | Probably unusable | Top of the review queue (stays Bronze) |

      **Weakest link:** each metric gets its own level from its cut-offs, and the clip's level is
      the lowest of them, so no weights are needed and one bad signal is enough to send a clip for
      review. `band_reason` lists the metric(s) at that lowest level, e.g. `cer:2` or
      `compression_ratio:1,word_count:1`, so a reviewer knows what to listen for. A metric that
      doesn't apply or is missing (CER outside sentences, `avg_logprob` for long-form clips) is
      skipped. Tier 2 never rejects: even level 1 waits for a human. CER and WER use `jiwer`
      (the version used in the model comparison) and the training normaliser.

      **Cut-offs (provisional, `thresholds_version` 2026-11-10-provisional; to be calibrated on
      pilot data by listening to a sample per level and task type):**

      | Metric | Applies to | 4 | 3 | 2 | 1 |
      |---|---|---|---|---|---|
      | `cer` | sentences | ≤ 0.10 | ≤ 0.15 | ≤ 0.40 | > 0.40 |
      | `has_digits` | sentences | — | — | true caps the clip at 2 | — |
      | `word_count` | all | — | — | under 5 words (spontaneous types) | 0 words |
      | `compression_ratio` | all | ≤ 2.0 | ≤ 2.4 | — | > 2.4 |
      | `words_per_second` | all | 0.8–3.5 | 0.5–0.8 or 3.5–4.5 | under 0.5 or over 4.5 | — |
      | `avg_logprob` | all (if present) | ≥ −0.5 | −1.0 to −0.5 | under −1.0 | — |
      | `snr_db` | all | ≥ 20 | 10–20 | under 10 | — |
      | speech ratio | all | ≥ 0.4 | 0.2–0.4 | under 0.2 | — |
      | `mean_dbfs` | all | ≥ −35 | −40 to −35 | under −40 | — |

      `wer` and `length_ratio` are reported but set no level (CER already covers them; the length
      ratio explains a high CER). Sources for the starting values: the 2026-11-05 model comparison
      for CER (matching clips 0.00–0.26, wrong sentences 0.43–0.92); Whisper's own defaults for
      compression ratio (2.4) and log-probability (−1.0); clean-audio SNR from 20 dB as used for
      NaijaVoices; the arrival check's 20% for the speech ratio. Cut-offs live in a config file
      with the version, so a change is a re-score, not a code change.
    - **On demand:** `scripts/score_tier2.py` scores clips not yet scored, re-scores after a cut-off
      change (`--rescore`), prints the report from stored results (`--report-only`) and writes a
      listening sample per task type and level (`--sample N`) for calibration (`docs/deploy.md`).

### 8.4 Tier 3: human review

- **Dashboard.** Supervisors log in to a web dashboard (PHP/MySQL, hosted on Hostinger) that shows
  their assigned contributors' recordings and plays the audio. It reads from the server through a
  token-protected gateway (`/v1/supervision/*`), using a read-only database account and the audio
  folder mounted read-only. Only participant IDs, status and batch leave the server, never names or
  phone numbers; audio is passed through on playback and not stored on Hostinger. Live for viewing
  and testing during the pilot.
- **What supervisors review:**
  - clips flagged by tier 1 (`needs_review`, e.g. `low_speech`, `speaker_check_pending`);
  - clips at tier-2 levels 1 and 2, lowest first;
  - a random spot-check sample of Silver clips, at a configured rate with a daily cap.
- **Decisions:** `accept` or `reject`. A rejection needs a reason from a fixed list:
  `wrong_sentence`, `noise`, `not_own_voice`, `unclear`, `not_chichewa`, `other` (with a note).
  Escalations and follow-ups with contributors are kept in the dashboard; they are not QC decisions.
- **Where decisions are recorded:** `qc_human_reviews`, one row per decision (recording, decision,
  reason, note, reviewer, time, a unique operation key so a retried request is never saved twice,
  and when the contributor was told). Rows are only added, never changed; a reversal is a new row,
  and the latest row is the current verdict. A trigger copies it to `recordings.qc_tier3` and
  `qc_tier3_reason`. The dashboard's database account may only add rows to this table; it has no
  rights on `recordings`.
- **Status (2026-11-10):** the dashboard's first version wrote decisions directly into
  `qc_status`; this was switched off before any decision was written. `qc_human_reviews` and its
  triggers come with the next migration; until then decisions are not recorded on the server.
- **Contributors are told about rejections:** a daily digest per contributor (which clips, why,
  minutes left), within WhatsApp's 24-hour window or as an approved template (planned). A clip
  rejected by a supervisor stops counting toward the contributor's minutes only once they have been
  told (`notified_at`), so minutes never drop without explanation. Rejected clips are not
  re-recorded; spare prompts cover the shortfall.

### 8.5 Final label

`recordings.qc_final`, set by a database trigger whenever a tier column changes. Every clip has
exactly one label.

| Label | Meaning | Use in the dataset |
|---|---|---|
| **Bronze** | Stored and passed tier 1, not yet validated: waiting for scoring, or waiting for a supervisor | Not released until validated |
| **Silver** | Validated automatically (tier-2 level 3 or 4), not reviewed by a human | Training data |
| **Gold** | Validated by a human (`qc_tier3 = accept`) | Training data; the only source for dev and test sets |
| **Rejected** | Not usable, with the reason and the tier that decided | Not released; kept |

Rules, applied in order (the first that matches decides):

1. A human decision wins (the latest one): `accept` → Gold, `reject` → Rejected.
2. Tier 1 rejected (`qc_status = rejected`) → Rejected.
3. Tier 1 flagged (`qc_status = needs_review`) → Bronze until a supervisor decides (proposed; e.g.
   forwarded clips waiting for a speaker check).
4. Tier-2 level 3 or 4 → Silver.
5. Otherwise Bronze: tier-2 level 1 or 2 (waiting for review) or not yet scored.

Clips only move up by automatic checks; only a human moves a clip to Gold or rejects a Silver or
Gold clip.

- **Measuring Silver quality:** the share of spot-checked Silver clips that supervisors reject
  estimates how trustworthy Silver is (reported per round and per contributor, and in the dataset
  card), and shows whether tier-2 cut-offs need tightening.
- **Two minute counts:** *collected minutes* (contributor progress, the bot's rule: everything not
  rejected by tier 1, minus notified human rejections) and *usable minutes* (Gold + Silver).
- **Precedents:** the three outcomes mirror Common Voice's validated / invalidated / other; ASR-based
  automatic acceptance follows LibriSpeech (clean/other by ASR error) and GigaSpeech (WER caps for
  training subsets); human-checked dev/test sets follow GigaSpeech; random per-reviewer re-checks
  follow NaijaVoices. Unlike Common Voice's two crowd votes, one trained supervisor decides, as in
  NaijaVoices and WAXAL.

### 8.6 Planned checks

- **Daily batch job (CPU):** speech/noise detection with a stronger voice activity model (e.g.
  Silero VAD) to catch loud non-speech clips; speaker-consistency checks (uploaded and forwarded
  clips first); the duration report. Results feed supervisor follow-up and the review queue.
- **Speaker check:** compare uploaded and forwarded clips (and later all clips) with a voice profile
  built from the participant's own recorded voice notes (speaker embeddings); also near-duplicate
  checks.
- **SNR cross-check:** compare our WADA-SNR values with an established implementation on 10–20 real
  clips (our lookup table was regenerated from the paper's model).
- All QC thresholds are calibrated on pilot data.

## 9. Privacy and data protection

- Privacy notice for participants (draft) covers what is collected, purpose, dataset use, sharing,
  retention, security and participants' choices (stop at any time; deletion on request).
- Phone numbers and names are restricted; logs never show full phone numbers, message text, media
  URLs or WhatsApp message IDs.
- Secrets live only in environment files on the server; never committed.
- **Processors outside the server:** Modal (GPU transcription: audio passed in each call, not kept;
  only model weights are cached) and Hostinger (supervision dashboard: participant IDs, recording
  metadata and supervisor decisions; audio passed through on playback, not stored). Both to be
  listed in the privacy notice.
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
| 2026-11-07 | Older/forwarded voice notes accepted for free speech only, flagged for speaker check; other task types still require a new recording |
| 2026-11-07 | Uploaded audio files follow the same rule as forwarded voice notes: accepted for free speech only, flagged for speaker check; rejected (`not_recorded`) in all other task types and outside a session, with a request for a new voice note and no retake used |
| 2026-11-07 | Per-task clip limits: sentences flagged > 30 s, rejected > 45 s; pictures/questions/free speech up to 60 s, older voice notes/uploads in free speech up to 120 s; aim 15–30 s; long clips kept, flagged and split before training; ASR uses long-form transcription for clips > 30 s (being tested; until then the ASR batch skips them). Supersedes the planned "read faster, then skip" handling of too-long sentences |
| 2026-11-08 | Long-form ASR enabled for clips over 30 s (tested with both models on a 90 s clip: about 0.1 real-time factor, nothing cut off); segment texts joined with a space; `transcripts.long_form` filled; the ASR batch no longer skips long clips |
| 2026-11-07 | Long, mostly silent clips: over 30 s with under 20% active audio rejected as `no_speech` (retake), so they can't count in full |
| 2026-11-09 | Registration welcome: a utility template (`registration_welcome`, first name and total target in minutes) sent once to each new participant after import, since they haven't written to the bot yet; the only paid message type (under about US$0.01 each); exempt from the 24-hour check, which still applies to all other messages |
| 2026-11-10 | ASR step 2 metrics agreed: audio (duration, speech ratio, mean level, SNR), transcript (word count, words per second, average log-probability, compression ratio), sentences (CER main, WER, length ratio, digits flag; prompts with numbers capped at review). Audio facts stay on `recordings` (new `snr_db`); transcript-based metrics go into a new `qc_results` table (one column per metric), one row per scoring run; the band is mirrored to a new `recordings.qc_tier2`. `qc_status` is tier 1 and is not changed by later tiers. Clipping not measured. The batch never rejects on its own |
| 2026-11-10 | QC protocol: three tiers plus a final label. Tier 1 `qc_status` (never overwritten), tier 2 `qc_tier2` 1–4, tier 3 `qc_tier3` accept/reject with a reason list (decisions in an append-only `qc_human_reviews`, copied by trigger), final `qc_final` Bronze/Silver/Gold/Rejected by trigger with a fixed precedence (human, tier-1 rejection, tier-1 flag, tier 2). Gold = human-validated; dev/test sets from Gold only; Silver quality measured by spot checks. Based on Common Voice, LibriSpeech, GigaSpeech, NaijaVoices |
| 2026-11-10 | Supervision dashboard (Hostinger, read-only gateway on the server) live for viewing; its direct write-back to `qc_status` switched off before use; decisions to be recorded in `qc_human_reviews`. A supervisor-rejected clip stops counting once the contributor is notified |
| 2026-11-10 | Tier-2 levels 1–4 (4 good, 3 borderline, 2 review, 1 probably unusable), combined by weakest link across metrics with the limiting metric(s) recorded; provisional cut-offs in config, calibrated on pilot data |
| 2026-11-08 | Bot instructions shortened, since contributors get a full guide: three general tips (quiet place and pauses, swipe-reply to redo, PROGRESS) and one or two lines per task type with its length limit; no change to behaviour |
