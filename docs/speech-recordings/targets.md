# Recording targets and collection runs

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
