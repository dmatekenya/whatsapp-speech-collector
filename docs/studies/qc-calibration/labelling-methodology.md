# QC calibration: labelling methodology (Stage 2)

Stage 2 labels the staged clips and a stratified sample of real pilot clips, so that the automatic
checks can be calibrated against human judgement. The step-by-step platform setup is in the
{doc}`labelling setup <labelling-setup>`.

## Pilot sample

About 300 pilot clips from real contributors (no test accounts, no study clips), stratified by task
type and tier-2 level, excluding arrival rejections:

| Task type | Clips | How drawn |
|---|---|---|
| Reading sentences | about 150 | As many different sentences as possible; up to 30 per tier-2 level |
| Pictures, conversation starters, free speech | about 50 each | Up to 10 per tier-2 level |

Stratifying matters: a random sample would be mostly good audio and say little about where the
boundaries should be. Staged clips are labelled first; the pilot sample is added before or during
labelling, and its timing is noted in the results because rater standards can drift.

## Labels

Labelling has two passes, so the audio judgement is not influenced by the prompt. The target is
**usefulness for ASR training**, not listening comfort: a clip is good if its main speaker's words
can be recovered reliably and nothing else is mixed in.

### Pass 1: audio labels (all clips, blind)

Raters hear only the audio: no condition, task type, prompt, transcript, speaker or tier-2 level.
Clips have random IDs and are presented in random order. Four questions:

**1. Who can you hear speaking?** (one answer)

| Option | Meaning |
|---|---|
| One main speaker | One clear voice the recording is focused on; others may be audible in the background |
| Two or more main speakers | Two or more people talking to the phone (e.g. a helper and the speaker) |
| Only speech from a radio, TV or phone (no live speaker) | A presenter, advert or recorded voice played from a device |
| Only music (no one speaking) | Music on its own |
| Only background voices (no main speaker) | Voices, but none the recording is focused on |
| Nobody is speaking | Silence or noise only |
| Not sure | |

Questions 2 and 3 are shown only for "One main speaker" or "Two or more main speakers".

**2. Could you write down what was said?** (transcribability; one answer)

| Option | Meaning |
|---|---|
| All | Every word clear |
| Most | One or two words would be a guess |
| Some | The meaning is clear but whole phrases are lost |
| Few or none | What was said cannot be written down |

Raters judge whether the sounds are clear, not whether they know the word.

**3. Language** (one answer): Chichewa (everyday borrowed words count as Chichewa) / Chichewa mixed
with another language (whole phrases) / Mostly another language / Can't tell.

**4. What problems can you hear?** (tick all that apply)

| Tag | Tick when… |
|---|---|
| No issues | Nothing wrong |
| Background noise (not voices or music) | Fan, generator, traffic, rain, TV hum, animals, banging, crowd rumble |
| Wind | Wind blowing on the microphone |
| Handling noise | Rubbing, knocks or scraping from the phone being moved or held |
| Background voices (words not understandable) | Other people talking; their words can't be made out |
| Competing speech (another person's words understandable) | Another person's words can be followed alongside the main speaker |
| Background music | Music, singing or a radio song behind a live speaker |
| Echo | The voice rings, as in a big empty room |
| Far from phone | The main speaker sounds distant in quiet surroundings |
| Too quiet | The voice is faint |
| Muffled | Dull, as if the microphone is covered |
| Distortion | Crackling, buzzing, harsh or broken-up voice |
| Cut off | Starts or ends mid-word, or has gaps |
| Other | Anything else, with a note |

For background sounds the rule is: people talking → background voices or competing speech; tune,
beat or singing → background music; anything else → background noise. Wind and handling noise come
from the phone itself. The tags say what kind of problem there is; transcribability says how much
it matters.

### Derived label: usable for ASR training

Raters are not asked whether a clip is "usable"; it is computed from their answers, so the
definition is explicit, identical for every clip, and can be revised without re-labelling:

> **Usable** = Q1 is "One main speaker"
> AND transcribability is "All" or "Most"
> AND language is "Chichewa" (or mixed, to be decided)
> AND none of the tags "Competing speech" or "Cut off".

Two or more main speakers are not usable for single-speaker transcripts. "Most" may be tightened
to "All" for read speech. The rule and any change to it are recorded in the decision log.

### Pass 2: transcription (pilot sentence clips only)

Each pilot sentence clip in the sample (about 150) is transcribed **verbatim**, without the
transcriber seeing the prompt or the ASR output: what was said, including misreadings, repeats and
skipped words, with unclear words marked `[?]`, plus a confidence choice (sure of every word /
unsure of some words). About 20% are transcribed twice to measure transcriber disagreement.

Staged sentence clips are **not** transcribed: every staged reading is the same sentence, so a
transcriber would soon know it by heart and hear the prompt rather than the audio.

With the human transcript, each pilot sentence clip has three texts:

| Comparison | Measures |
|---|---|
| prompt vs human transcript | speaker error (misreading, skipped or added words) |
| human transcript vs ASR | ASR model error |
| prompt vs ASR | total CER, which is what tier 2 computes |

## Raters

- **Three raters.** Either every rater labels every Pass 1 clip (three labels per clip; majority
  answer, no tie-break needed), or clips are split into three batches each labelled by a different
  pair (two labels per clip, about two-thirds of the clips per rater). The choice depends on rater
  time; both allow agreement between every pair of raters.
- **Training:** a joint session on about 20 clips with one example per issue tag, using staged
  clips as gold examples (e.g. C9 must be "Nobody is speaking").
- **Gold clips:** staged clips with an unambiguous condition are seeded through the queue to check
  raters stay consistent.
- **Blinding:** people who listened to staged clips with their condition labels (SC and Gloria in
  the dashboard check) do not rate staged clips.
- **Agreement:** Krippendorff's alpha (ordinal) for transcribability, Cohen's kappa and percent
  agreement for the derived usable label, and transcriber CER on the double-transcribed clips.
  Targets: alpha ≥ 0.70, kappa ≥ 0.60. If agreement is lower, the definitions or training are
  revised before the labels are used.
- **Effort:** under a minute per clip for Pass 1; a few minutes per clip for Pass 2. Sessions of
  about 45 minutes.

## Labelling platform

Labelling runs in **Label Studio** (Community Edition, open source), self-hosted on the droplet at
`https://label.zazoai.com` as a separate Docker Compose project behind the bot's Caddy. Details and
commands are in the {doc}`labelling setup <labelling-setup>`.

- **Blind bundles:** `scripts/labelling/build_labelling_bundle.py` copies the selected WAVs under
  random 12-character IDs into `data/labelling/bundles/<name>/`, writes the Label Studio task files
  for Pass 1 and Pass 2, and writes a **private key** linking each ID to recording, speaker,
  condition and tier-2 level. Only the bundle folder is visible to Label Studio (read-only); the
  key never leaves the droplet.
- **Forms:** `config/labelling/qc_calibration_pass1.xml` (the four questions, with questions 2–3
  shown only when there is a main speaker) and `config/labelling/qc_calibration_pass2.xml`
  (transcription). Option wording is frozen once real labelling starts; new options may be added
  but existing ones are not renamed.
- **Projects:** one project per pass. Task sampling is random, so each rater gets a different
  order. Audio is served from a Label Studio *Local files* source storage pointing at the bundle
  folder; tasks are imported from the JSON task files, not synced.
- **Raters** join by invite link with individual accounts; every annotation records who made it,
  when and how long it took. The Community Edition cannot assign specific clips to specific raters,
  so batch splits are done with separate projects.
- **Export:** labels are exported as JSON and joined to the private key on the droplet for
  analysis.
