# Transcription quality assurance

- **Step 2, scoring (in place):** measures every transcribed clip on the droplet's CPU, with
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
  - **Levels from CER only, for sentences only (since 2026-11-11, `thresholds_version`
    `2026-11-11-cer-only`):** on the full pilot (929 clips) only CER separated clips reliably.
    WADA-SNR saturates at 100 dB on WhatsApp audio (its noise suppression), the compression
    ratio flags long normal answers, and log-probability and level never triggered. So:
    - sentences get a level from CER alone (cut-offs below), still capped at 2 when the prompt
      has digits;
    - image, conversation-starter and free-speech clips get **no level**: their `qc_results` row
      has every metric, `band` empty and `band_reason` `not_applicable`, and `qc_tier2` stays
      empty ("not applicable" in the report);
    - every other metric is still computed and stored in `qc_results` for every clip, as
      report-only, to calibrate against supervisors' decisions later. Whether a metric sets a
      level is a setting per metric in `config/qc_tier2.yaml` (`sets_level`), so one can be
      switched back on with a re-score and no code change.
  - **Tier-2 level (`qc_tier2`):** a whole number from 1 to 4 (empty = not scored yet, or not
    applicable), stored as `qc_results.band` and copied to `recordings.qc_tier2`. Each level leads to a different
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

    **Cut-offs (first set 2026-11-10, `2026-11-10-provisional`; to be calibrated on pilot data
    by listening to a sample per level and task type). Since 2026-11-11 only the `cer` and
    `has_digits` rows set a level; the others are kept in the config, switched off:**

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
    listening sample per task type and level (`--sample N`) for calibration (`docs/platform/deployment.md`).

Final recording labels and supervisor decisions are defined in {doc}`/speech-recordings/quality-assurance`.
