# Transcription methodology

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

Human transcription and correction procedures are pending; see {doc}`/transcription/manual`.
