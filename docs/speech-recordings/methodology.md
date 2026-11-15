# Speech-recording methodology

## Participants

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
  request), and not to participants who have already written to the bot. Successful imports send
  it automatically to new, active, non-test participants; dry runs send nothing, and
  `--no-send-welcome` skips it for one import.
- **Only registered numbers take part.** An unregistered number gets one reply saying so; none of
  its messages or recordings are kept.
- **Identity in the data.** Recordings are linked only to the participant ID; names and phone
  numbers stay in the restricted registry.

## Session design

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
- **Retakes:** clips that are too short, too long (see Clip limits below), empty or
  without speech get a retake request (up to 2 retakes per prompt); failed attempts are stored as
  rejected. Outside free speech, a forwarded
  voice note or an attached audio file gets a request to record a new voice note, but does not use
  up a retake: the prompt stays open. In free speech both are accepted (see Audio handling below), so the
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

## Clip limits

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
  need long-form ASR (see the transcription methodology).

## Audio handling

- Each voice note is stored as the original (`.ogg`) plus a 16 kHz mono WAV for QC and ASR, in a
  per-participant folder; files never move. Accepted and rejected sets are produced by export.
- Exact duplicates (re-sent audio) are detected by content hash and rejected (`duplicate`, since
  2026-11-11): in a session the prompt is sent again (no retake used); they never count.
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
