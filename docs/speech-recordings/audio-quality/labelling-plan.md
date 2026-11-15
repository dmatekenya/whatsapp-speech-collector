# Audio quality labelling plan

Last updated: 2026-11-11. Part of the audio-quality experiments ([handover.md](handover.md)).

Three raters score about 540 clips (3 hours) for audio quality on four simple questions, over 2–3
days. The scores calibrate the automatic tier-2 checks and form a labelled Chichewa audio-quality
set that may later be released openly.

## The pool

| Source | Clips | Audio | How chosen |
| --- | --- | --- | --- |
| Recorded test clips | ~360 | ~2 h | All of them: 10 speakers × 36 clips in 11 categories (see [recording-guide.md](recording-guide.md)) |
| Pilot clips | ~180 | ~1 h | Half at random, half with the weakest tier-2 metrics; 30 seconds or shorter; spread across contributors and task types |
| **Total** | **~540** | **~3 h** |  |

- **Blind and shuffled:** raters don't know which clips were staged; the order is random and
  different for each rater.
- **Every rater rates every clip,** so each clip gets 3 independent ratings and rater agreement can
  be measured.
- The recording category and the tier-2 metrics are kept with each clip but never shown to raters.

## What raters answer

Listen to the whole clip once (replay if needed), then answer in this order. The three scales follow
ITU-T P.835, the standard for rating noisy speech.

**0. Is someone speaking?** Yes / No. If no, stop here and go to the next clip.

| Score | 1. Voice clarity: the voice itself | 2. Background: other sounds | 3. Overall quality |
| --- | --- | --- | --- |
| **5** | Perfectly clear and natural | Not noticeable | Excellent: every word easy, nothing distracting |
| **4** | Clear, slightly soft, distant or dull | Slightly noticeable | Good: small issues, nothing lost |
| **3** | Noticeably unclear (quiet, distant, echoey, muffled or distorted), but understandable | Noticeable, but not intrusive | Fair: clear problems, but every word can be understood with attention |
| **2** | Hard to understand in places | Somewhat intrusive | Poor: some words lost or guessed; tiring |
| **1** | Mostly not understandable | Very intrusive | Bad: most of the speech can't be followed |

- **Judge the voice and the background separately.** A clear voice with loud music is voice 5,
  background 1. A muffled voice in a quiet room is voice 2, background 5.
- **Overall is your own judgement,** not the average of the other two. Ask: could every word be
  written down?
- **Rating sheet,** one row per clip: clip ID, play link, speech (Y/N), voice, background, overall,
  note (optional).

## Rater training and quality checks

- **Raters:** 3 people, fluent in Chichewa, with headphones, in a quiet place. Not the people who
  recorded the clips.
- **Training (about 1 hour):** go through this page, then listen to 1–2 example clips for each score
  on each scale, picked by Dunstan or Stewart from the recorded clips with clear-cut answers. Then
  rate 15 practice clips and compare with the agreed answers before starting.
- **Hidden checks during rating:**
    - **Silence clips** (categories C10 and C11): the answer to question 0 is certainly no.
    - **About 20 repeated clips,** appearing twice without notice: the two ratings should be within
      1 point.
- **Pace:** 2–3 hours a day, with a short break every hour. Ratings drift in long sessions.
- A rater who often fails the checks gets a short refresher; their ratings so far are reviewed
  before use.

## Combining the ratings and how they are used

| Question | Combined from 3 raters as |
| --- | --- |
| Speech? | Majority vote |
| Voice clarity, background, overall | Average of the three scores (mean opinion score, MOS) |

- **Every individual answer is kept,** not just the averages, to measure agreement and for any
  public release.
- **Usable for training** = overall MOS of 3 or more (every word can be understood). This is the
  main target for calibrating tier 2.
- **Calibration:** check which automatic measures (speech ratio, level, log-probability, compression
  ratio, DNSMOS, Brouhaha) predict low overall and background scores, per recording category and
  phone, and set the tier-2 audio cut-offs from that. Silence clips calibrate the arrival speech
  check (tier 1).
- Clips where raters differ by 2 or more points are listened to again by Dunstan or Stewart; they
  mark where the boundary between levels lies.

## Timeline and effort

About one working week from recording to labelled data; the rating itself takes 2–3 days.

1. **Day 1:** speakers record the test clips (about 1 hour each, same day).
2. **Day 2:** prepare the pool: transcribe, pick the pilot clips, shuffle, build the rating sheet
   (half a day).
3. **Day 3:** rater training (about 1 hour each).
4. **Days 3–5:** rating, all three raters in parallel. Each listens to the full 3 hours plus about 15
   seconds per clip to answer: about 5.5 hours of work, 6–7 hours with replays and breaks.
5. **Day 6:** check agreement, re-listen to large disagreements, then calibrate.

Total rating effort: about 16–20 rater-hours.

## Public release (later)

No labelled audio-quality data for Chichewa or Malawian phones appears to exist; this set could be
the first, as an evaluation and calibration set.

- **Consent:** recorded clips only from speakers who agreed in writing to open publication; pilot
  clips only if the contributors' consent covers it.
- **Per clip:** audio, recording category (recorded clips), phone brand and model, speaker's first
  language, pseudonymous speaker ID; no names or phone numbers.
- **Ratings:** every rater's individual answers with pseudonymous rater IDs, plus the averages.
- **Licence:** to match the main dataset (suggested CC BY 4.0).
