# QC calibration study: methodology

Last updated: 2026-11-24. This page brings the study together. It includes the
{doc}`recording methodology <recording-methodology>` (Stage 1) and the
{doc}`labelling methodology <labelling-methodology>` (Stage 2); edit those files, not the copies
shown here. How clips were recorded is in the {doc}`recording protocol <protocol>`, and the platform
setup in the {doc}`labelling setup <labelling-setup>`.

## 1. Purpose

Tier 2 (the ASR batch checks) gives each clip a level from 1 to 4 using provisional cut-offs. Those
cut-offs came from literature defaults and a handful of test clips. This study produces the
evidence to replace them, on two dimensions:

- **Audio quality:** can the main speaker's words be recovered, or are they degraded by noise,
  other voices, music, echo, distance, low level, obstruction or silence?
- **Transcript fidelity (reading sentences only):** does what was said match the prompt, and when
  the ASR transcript does not match, is that the speaker's error or the model's?

The study changes no tier-1 rule, and its labels are not tier-3 decisions (section 6).

## 2. Questions

1. Which recording conditions make a clip unusable for ASR training, and how severely?
2. How well does each audio measure (noise floor, level range, speech ratio, spectral centroid)
   separate usable from unusable clips, and where should its level boundaries sit?
3. For reading sentences: at what CER should a clip be accepted automatically, sent for review,
   or treated as a likely wrong or unfinished reading? How much of the CER comes from the speaker
   and how much from the ASR model, and does that change with the recording condition and the
   sentence?
4. For spontaneous clips: do the transcript signals (`avg_logprob`, `compression_ratio`,
   `words_per_second`, `word_count`) flag clips that humans cannot transcribe?
5. Overall: how often do humans agree with the tier-2 level? In particular, what share of level
   3–4 (Silver) clips are unusable, and what share of unusable clips land at levels 1–2?

## 3. Design

| Stage | What | Clips |
|---|---|---|
| 1. Staged recordings | Speakers record under conditions C1–C10 through the bot's study mode | 30 per speaker |
| 2. Labelling and calibration | Raters label the staged clips and a stratified sample of real pilot clips; labels are compared with the automatic measures | staged clips + about 300 pilot clips |

The staged set has a known condition for every clip and covers poor audio on purpose, which real data
rarely does, but its conditions are acted and it uses one sentence and one question. The pilot set
has varied prompts and natural conditions. Cut-offs are fitted on the pilot clips; the staged clips
check that each measure responds to the problem it should catch, and serve as gold examples.

## 4. Recording and labelling

```{include} recording-methodology.md
:heading-offset: 2
```

```{include} labelling-methodology.md
:heading-offset: 2
```

## 5. Analysis

**Reference label.** The derived usable label (labelling methodology), using the majority answer across
raters.

**Conditions (Q1).** Share usable and transcribability per condition on the staged set; tag
frequencies per condition, to check raters hear what was staged. Order effects: labels by order
position.

**Audio measures (Q2).** For each measure, the distributions for usable and unusable pilot clips
and the ROC AUC. Cut-offs are set after fixing the review budget (e.g. the share of clips humans can
review): the auto-accept boundary so that at least about 95% of auto-accepted clips are usable, and
the "probably unusable" boundary so that very few usable clips fall below it. The noise floor is the
first candidate, with −50 dBFS as the provisional review threshold. Targets are agreed before
cut-offs are set, not tuned afterwards.

**Transcript fidelity (Q3).** CER cut-offs for sentences are set so that correctly read clips with
usable audio pass and wrong or unfinished readings do not. Model error is broken down by condition
(staged set) and by sentence features such as length, loanwords and digits (pilot set).

**Spontaneous signals (Q4).** Same approach as Q2 for the transcript signals, against
transcribability.

**Combined level (Q5).** All labelled clips are re-scored with the new cut-offs, reporting the share
of level 3–4 clips that are unusable, the share of unusable clips at levels 1–2, and the expected
review-queue size, compared with the provisional cut-offs. A simple model (logistic regression or
gradient boosting on the signal and transcript measures, cross-validated by speaker) is trained on
the derived usable label if the cut-offs alone are not sufficient.

## 6. Outputs and data handling

- A new tier-2 `thresholds_version` with the evidence for each cut-off, recorded in the main
  methodology and its decision log; the pilot is re-scored with it.
- A calibration report: per-condition results, measure distributions, CER decomposition, agreement.
- The staged clips with their labels as an open audio-quality dataset, if the consent form covers
  it (staged clips only).
- Staged clips are kept out of production totals, lifetime minutes, prompt coverage and dataset
  exports (study enrollments; `main_recordings`).
- Labels live in Label Studio and its exports, not in `qc_human_reviews`, so they do not change any
  clip's tier-3 decision or final label. Whether pilot-clip labels are later imported as tier-3
  decisions is a separate decision.
- Bundle folders (audio copies) are deleted when labelling is finished; the private key is kept
  with the study data.

## 7. Limits

- Staged conditions are acted and may be more extreme or more uniform than real ones; the staged
  C1 condition was noisier than typical pilot recordings.
- WhatsApp's processing (gain control, noise suppression, compression) changes the audio before we
  receive it, for both the measures and the raters.
- One sentence and one question in Stage 1; prompt variety comes only from the pilot set.
- Speakers are pilot participants who already know the task.
- Ratings are subjective; agreement is measured and reported.
- With fewer than 16 speakers, language groups are not compared.

## 8. Open items

- Final choice between three raters on all clips or pairs on three batches.
- The language part of the usable rule (mixed allowed or not), and "Most" vs "All" for read
  speech.
- Whether staged sentence clips get a "read correctly" judgement.
- The labelling guide for raters (to be added to this study's documents).
