# QC calibration: recording methodology (Stage 1)

Stage 1 produced staged clips under known recording conditions. How the recordings were made is in
the {doc}`recording protocol <protocol>`; this page explains the design choices.

## Why staged recordings

Real recordings rarely contain clearly bad audio, and when they do, the cause is unknown. Staged
recordings cover each common problem on purpose and label every clip with the condition that was
requested. They are used to check that each automatic measure responds to the problem it is meant to
catch, and as gold examples for raters. Thresholds are fitted on real pilot clips (Stage 2).

## Conditions

The ten conditions cover the problems most likely in WhatsApp voice notes recorded at home or in
public places: a clean reference (C1), a quiet voice (C2), distance (C3), three kinds of background
(C4 voices, C5 music, C6 non-speech noise), echo (C7), a covered microphone (C8), and two kinds of
clips with no speech from the participant (C9 quiet silence, C10 silence with others talking).

C3 was originally planned with the phone 1–2 m away and no reading task, because the speaker could
not read the screen at that distance. It was changed to arm's length (about 60–70 cm), which is
practical, realistic for how some contributors hold the phone, and lets the speaker read, so C3 has
the same three tasks as C1–C8. Clips recorded before the change were kept; the restored sentence
task was added for those speakers.

## Fixed prompts

Every speaker read the same sentence (S12577, a 30-word sentence on poultry farming, chosen for
everyday vocabulary, no numbers and no names) and answered the same question (C0482, on how the fuel
shortage affects daily life, chosen because it is relatable and gets people talking, phrased around
daily life rather than politics). Keeping the prompts fixed means the condition is the only thing
that changes between a speaker's clips. The cost is that ASR-based thresholds for reading are set on
one sentence; varied sentences come from the pilot clips in Stage 2.

## Controls

- **Order:** each speaker had a randomised suggested condition order, but could pick any condition.
  The actual order position is stored per clip, so practice effects can be checked.
- **Reminders:** the bot repeated the condition in every prompt and after breaks, to avoid clips
  recorded in the wrong setup.
- **Durations:** soft duration targets with feedback; clips were never rejected.
- **Re-records:** allowed and kept; the latest attempt per task is labelled.

## Sample

Planned: 16 speakers, four per first-language group (Chichewa, Tumbuka, Sena, Yao), two men and two
women in each, each recording 30 clips (480 clips, about 3.2 hours). The fallback was 10 speakers,
not to be used for comparing language groups.

Actual: 12 speakers were enrolled for Stage 1 (seven women, five men; first languages Chichewa 7,
Tumbuka 3, Lomwe 1, Nyanja 1; no Sena or Yao speakers), recruited from the pilot and the team's
networks. Team test accounts were excluded from the analysis. Language groups are therefore not
compared. Final clip counts per speaker and condition are reported with the Stage 2 results.

## Preliminary signal analysis

A first look at simple signal measures on the staged clips, compared with the same speakers' pilot
clips, found:

- **Loudness does not separate conditions.** Phones and WhatsApp apply automatic gain control, so
  "too quiet" (C2) and "arm's length" (C3) have about the same overall level as normal speech
  (about −19 dBFS).
- **The noise floor separates background conditions clearly.** The 10th percentile of 30 ms frame
  energy was about −43 to −35 dBFS for C4–C6, against about −72 dBFS for the same speakers' pilot
  clips. About 10% of pilot clips have a noise floor above −50 dBFS. WADA-SNR saturates on this
  audio and is not used.
- **Muffled audio (C8)** shows as a low spectral centroid. **Echo (C7) and arm's length (C3)** are
  hard to see with simple measures.
- **Silence with others talking (C10) looks like speech** to the energy-based speech check; only
  labels, the transcript or a speaker check can catch it.
- **The staged C1 was noisier than real pilot recordings** (noise floor about 11 dB higher), so the
  pilot clips are the clean reference, not C1.

These findings are descriptive; Stage 2 labels decide the cut-offs.
