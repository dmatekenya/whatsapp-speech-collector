# Domain scoring and evaluation

A vision model suggests one primary domain per kept candidate, using the manual's rule
(central visible activity). Suggestions are stored separately from candidates and are hidden
from annotators by default; the annotator's domain is final.

## Setup

Add to `.env`:

```
OPENROUTER_API_KEY=...
```

Check the exact model IDs and prices, then edit `models:` in `config/picture-domain-scoring.yaml`:

```bash
python3 -m scripts.prompt_generation.score_picture_domains --list-models | grep -Ei "haiku|sonnet|gemini|gpt|qwen"
```

## Evaluation run (about 100 images, a few models)

1. Make the labelling page and label in a browser:

   ```bash
   python3 -m scripts.prompt_generation.picture_domain_eval sheet --run-id 2026-11-15-commons-test
   open data/prompts/images/harvest/2026-11-15-commons-test/label-sheet.html
   ```

   Pick one domain per image (or `unclear` / `not-relevant`), click **Export**, and save the file as
   `calibration-labels.csv` in the same run folder. Choices are kept in the browser if you
   stop halfway; regenerating the sheet pre-fills labels from the CSV.

2. Score only the labelled images, with and without title/tags:

   ```bash
   python3 -m scripts.prompt_generation.score_picture_domains --run-id 2026-11-15-commons-test --labelled-only --limit 10 -v
   python3 -m scripts.prompt_generation.score_picture_domains --run-id 2026-11-15-commons-test --labelled-only
   python3 -m scripts.prompt_generation.score_picture_domains --run-id 2026-11-15-commons-test --labelled-only --no-metadata
   python3 -m scripts.prompt_generation.score_picture_domains --run-id 2026-11-15-commons-test --labelled-only --larger
   ```

   Start with `--limit 10` to check that every model ID works and to see the cost per image.
   Spending stops at `max_spend_usd` (default $4) per command, using the cost OpenRouter
   reports for each call.

3. Compare with your labels:

   ```bash
   python3 -m scripts.prompt_generation.picture_domain_eval report --run-id 2026-11-15-commons-test
   ```

   The report (also saved as `domain-eval-report.md`) shows, per model and metadata setting:
   accuracy, top-2 accuracy, how often the model is confident (>= 0.8) and how accurate it is
   then, cost, the most common confusions, and flag counts per model/metadata setting. The first line gives the baseline: how often the
   domain of the search query is already right.

## Outputs (in the harvest run folder)

| File | Contents |
|---|---|
| `label-sheet.html` | labelling page |
| `calibration-labels.csv` | your labels: `candidate_id, label_domain, notes` |
| `domain-scores.csv` | one row per (candidate, model, metadata setting): domain, second choice, confidence, reason, tokens, cost, status and four safety/quality flags |
| `domain-eval-report.md` | evaluation report |

Re-running a scoring command skips pairs that already have a valid score.

## Suitability and flags

`unclear` means a usable image has no clear domain. `not-relevant` means it should not be
used as a speaking prompt: an isolated posed portrait, a document/map/logo, an unrelated
scene or an image too poor to understand. The current scoring rubric allows public figures.
It reports `sensitive`, `graphic_or_sexual`, `visible_text` and `poor_quality` separately;
these are review signals and do not automatically drop or publish a candidate.

Scores append `flag_sensitive`, `flag_graphic_or_sexual`, `flag_visible_text` and
`flag_poor_quality` columns as lowercase `true`/`false` for successful replies. Missing or
non-boolean model flags default to false. Existing score files with the legacy header are
rewritten atomically before appending, retaining every row and leaving old flag cells blank.
Existing successful scores still skip on resume; blank legacy flags mean unassessed, not false.
The report counts the latest successful score per candidate/model/metadata setting, including
unlabelled images, without counting repeated successful rows twice.
