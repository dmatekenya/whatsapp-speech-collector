# SC calibration study contract

Use these objects for the calibration study. Existing supervision endpoints
and pages must stay unchanged. Study checks do not change existing QC review
history, `qc_final`, recording status, or participant progress.

## Read clips

`sc_readonly` and `masakhane_supervision_ro` can select from
`public.calibration_review_clips`. The view exposes one row per calibration
attempt and contains no participant names or phone numbers.

| Columns | Meaning |
|---|---|
| `recording_id`, `participant_id`, `enrollment_id` | Recording and study identifiers |
| `is_test` | Participant flag for showing or hiding test speakers |
| `condition_code`, `condition_name` | Calibration setup code and display name |
| `task_type`, `prompt_id`, `attempt`, `order_position` | Task, optional fixed prompt, attempt number, and condition order |
| `duration_s`, `active_seconds`, `mean_dbfs` | Stored recording measurements, where available |
| `wav_path`, `created_at` | WAV path relative to the audio root and time the calibration clip was created |
| `as_intended`, `reviewer`, `checked_at` | Current study check, or null before review |

Where a recording has more than one check, the latest `created_at` wins,
with the highest check ID breaking ties.

## Append a check

`masakhane_qc_writer` has `INSERT` and `SELECT` on
`public.calibration_clip_checks`. The database assigns `id` and `created_at`.
Use the authenticated reviewer's identifier and a fresh UUID operation key for
each new action. `note` is optional. Keep participant names and phone numbers
out of notes.

```postgresql
INSERT INTO public.calibration_clip_checks
    (recording_id, as_intended, note, reviewer, operation_key)
VALUES
    ($1, $2, $3, $4, $5)
RETURNING id, created_at;
```

Use `yes`, `no`, or `unclear` for `as_intended`. A recording must exist in
`calibration_clips`. Reuse the operation key for a retry. If PostgreSQL
rejects the duplicate, read the existing check by that key and verify it
matches the retried request. To correct a check, append a new row.
