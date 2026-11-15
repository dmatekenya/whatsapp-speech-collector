# Recording quality assurance

## Quality-control protocol

Each clip passes through up to three tiers of checks. Each tier records its own result in its own
column, and a fourth column combines them into one final label. No tier changes another tier's
column.

| Tier | When | Who | Recorded in | Values |
|---|---|---|---|---|
| 1. Arrival checks | on arrival | the bot | `recordings.qc_status`, `qc_reason` | `accepted`, `needs_review` (accepted, flagged for a supervisor), `rejected`; `pending` only until the bot has finished its turn (see Arrival checks below) |
| 2. ASR batch checks | after each transcription batch (twice a day in the pilot) | scoring on the server | `qc_results` (history), `recordings.qc_tier2` | sentences: 1 (probably unusable) to 4 (all signals good), from CER; empty = not scored, or not applicable (other task types) |
| 3. Human review | flagged clips and random spot checks | supervisors, in the dashboard | `qc_human_reviews` (history), `recordings.qc_tier3`, `qc_tier3_reason` | `accept`, `reject`; empty = no human decision |
| Final label | whenever a tier changes | database triggers | `recordings.qc_final` | `bronze`, `silver`, `gold`, `rejected` |

- **Only tier 1 and humans reject.** Tier 2 never rejects a clip on its own; low levels send it to
  a supervisor.
- **Tier 1 is never overwritten.** `qc_status` drives the bot's retakes and progress counts, so
  later tiers write only their own columns.
- **Rules live in one place:** the final label is set by database triggers from the tier columns
  (see Final label below), not by the bot or the dashboard.
- **Labels follow the data-lake convention of increasing trust:** Bronze → Silver → Gold. Here Gold
  means checked by a human, not aggregated as in a data warehouse.

## Arrival checks

- **On arrival (worker and conversation engine):** the worker measures duration and speech levels
  on the WAV and applies the default audio-origin rule and long flag; once the clip is matched to
  a prompt, the engine applies the task type's rules: audio origin, empty audio, duplicates,
  duration limits (see the recording methodology), speech check; retake requests.
- **Outcome (`qc_status`, since 2026-11-11):** the engine's turn always ends in one of three values,
  with the reason in `qc_reason`:

  | Value | Meaning | Reasons | Counted toward progress |
  |---|---|---|---|
  | `accepted` | Passed the arrival checks; also clips sent outside a session (never counted, as they answer no prompt) | none | yes |
  | `needs_review` | Passed, but flagged for a supervisor | `low_speech`, `speaker_check_pending` | yes |
  | `rejected` | Failed | `empty`, `too_short`, `too_long`, `no_speech`, `not_recorded`, `duplicate` | no |

  `pending` is the value while the turn runs: the worker stores the clip, then the engine checks it
  in a separate step, normally within a second. A clip still `pending` afterwards means the turn
  failed; the job is retried, and until then the clip is visibly unchecked (Bronze, 8.5).
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

## Human review

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
  `wrong_sentence` (not the sentence shown), `no_speech` (silence or no voice), `noise`
  (background noise drowns the speech), `unclear` (mumbled, cut off, distorted), `not_own_voice`
  (another speaker, or a forwarded clip of someone else), `not_chichewa` (another language), or
  `other` (with a note). Escalations and follow-ups with contributors are kept in the dashboard;
  they are not QC decisions.
- **Where decisions are recorded:** `qc_human_reviews`, one row per decision:
  - **Columns:** recording, decision, reason, note (required for `other`; at most 500 characters),
    reviewer ID and name, time, a unique operation key, and `notified_at` (when the contributor
    was told). The operation key means a retried request is never saved twice.
  - **Rules:** checked by the database, which refuses a rejection without a reason, a reason on an
    acceptance, `other` without a note, and unknown decisions or reasons.
  - **Append-only:** rows are only added. A database trigger refuses any change or deletion,
    whoever tries it (error code `QC405`). The one exception is setting `notified_at` once, from
    empty to a time. A reversal, or an administrator's correction, is a new row, and the latest
    row (`created_at`, then `id`) is the current verdict.
  - **Copied to the recording:** a trigger copies each new decision to `recordings.qc_tier3` and
    `qc_tier3_reason` in the same transaction, which in turn sets `qc_final` (8.5) and
    `updated_at`. `qc_status` is never changed.
  - **No overwriting each other:** the trigger locks the recording first. If the dashboard says
    which decision it expects to replace (the transaction setting `masakhane.expected_qc_tier3`:
    `accept`, `reject`, or `__NULL__` for none), a different current decision refuses the new one
    (error code `QC409`). Of two supervisors deciding the same clip at once, only the first is
    saved; the second is told to refresh.
  - **Rights:** the dashboard's database account may only add rows to this table (and read them);
    it has no rights on `recordings`. The trigger runs with the table owner's rights, which is
    what lets a decision reach `recordings`.
- **Status (2026-11-11):** the table and triggers are in place (migration `d9f4b2c8e631`). The
  dashboard's write-back stays off until it has been tested against them on a copy of the data.
  The dashboard's first version wrote decisions directly into `qc_status`; this was switched off
  on 2026-11-10 before any decision was written.
- **Contributors are told about rejections:** a daily digest per contributor (which clips, why,
  minutes left), within WhatsApp's 24-hour window or as an approved template (planned). A clip
  rejected by a supervisor stops counting toward the contributor's minutes only once they have been
  told (`notified_at`), so minutes never drop without explanation. Rejected clips are not
  re-recorded; spare prompts cover the shortfall.

## Final label

`recordings.qc_final`, set by a database trigger whenever a tier column changes. Every clip has
exactly one label.

| Label | Meaning | Use in the dataset |
|---|---|---|
| **Bronze** | Stored and passed tier 1, not yet validated: waiting for scoring, or waiting for a supervisor | Not released until validated |
| **Silver** | Validated automatically (tier-2 level 3 or 4), not reviewed by a human | Training data |
| **Gold** | Validated by a human (`qc_tier3 = accept`) | Training data; the only source for dev and test sets |
| **Rejected** | Not usable, with the reason and the tier that decided | Not released; kept |

Rules, applied in order (the first that matches decides). They live in one database function,
`qc_final_label()`, used by the trigger on `recordings`. The task type comes from the clip's
assignment.

1. **A human decision wins** (the latest one): `accept` → Gold, `reject` → Rejected. This
   overrides tier 1 too.
2. **Tier 1 rejected** (`qc_status = rejected`) → Rejected.
3. **Tier 1 flagged or not finished** (`qc_status` `needs_review` or `pending`) → Bronze until a
   supervisor decides (e.g. forwarded clips waiting for a speaker check), even at tier-2 level 4.
4. **Sentences:** tier-2 level 3 or 4 → Silver; otherwise (level 1 or 2, or not yet scored) →
   Bronze.
5. **Image, conversation-starter and free-speech clips** (tier 2 not applicable) → Silver.
6. **Unassigned recordings** (no assignment) → Bronze: never counted, never exported.

- **What Silver means for spontaneous speech (decision 2026-11-11):** a spontaneous clip accepted
  on arrival is Silver. It has passed the arrival checks (duration, speech, audio origin) but no
  automatic check of its content, because tier 2 has no reliable signal for spontaneous speech
  yet. Its quality rests on the arrival checks and on supervisors' spot checks, which measure it.
  This is revisited when tier 2 has such a signal.
- **When it changes:** `qc_final` is recomputed whenever `qc_status`, `qc_tier2`, `qc_tier3` or the
  assignment changes, and `updated_at` moves with it, so the dashboard's incremental sync sees
  the change.
- **Movement:** clips only move up by automatic checks; only a human moves a clip to Gold, rejects
  a Silver or Gold clip, or overrides a tier-1 rejection.

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

## Planned checks

- **Daily batch job (CPU):** speech/noise detection with a stronger voice activity model (e.g.
  Silero VAD) to catch loud non-speech clips; speaker-consistency checks (uploaded and forwarded
  clips first); the duration report. Results feed supervisor follow-up and the review queue.
- **Speaker check:** compare uploaded and forwarded clips (and later all clips) with a voice profile
  built from the participant's own recorded voice notes (speaker embeddings); also near-duplicate
  checks.
- **SNR cross-check:** compare our WADA-SNR values with an established implementation on 10–20 real
  clips (our lookup table was regenerated from the paper's model).
- All QC thresholds are calibrated on pilot data.

Automatic transcript scoring is documented in {doc}`/transcription/quality-assurance`.
