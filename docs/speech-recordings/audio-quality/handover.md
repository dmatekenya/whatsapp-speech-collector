# Audio quality experiments: handover

Last updated: 2026-11-11. Owner: Ange.

Record about 2 hours of deliberately varied audio, have it rated with 1 hour of pilot clips by 3
raters, and use the ratings to calibrate the automatic audio-quality checks. The set may later be
released as an open Chichewa audio-quality dataset.

Related: [recording-guide.md](recording-guide.md) (for the speakers) and
[labelling-plan.md](labelling-plan.md) (for the raters).

## Why this work

The automatic checks (tier 2, `docs/transcription/quality-assurance.md`) were run on the full pilot (929
clips, 5.6 hours) on 11 November. Only one signal was reliable:

- **CER of sentence readings against their prompt works** (median 0.08; wrong readings stand out
  clearly). Tier 2 now uses CER only.
- **Nothing reliably detects poor audio:** the WADA-SNR estimate sits at its maximum (100 dB) for
  most clips because WhatsApp's own noise suppression makes pauses nearly silent; model confidence
  and loudness barely vary; compression ratio flags long, normal answers.
- **The energy-based silence check on arrival (tier 1) misses silence with background noise,**
  because it counts any loud sound as speech.

So spontaneous clips (about 70% of the dataset) are currently accepted on arrival with no
audio-quality check, and supervisors can review only about 10% of clips. This work gives us the
labelled data to choose and calibrate a reliable audio-quality measure.

## What's decided

| Topic | Decision |
| --- | --- |
| Recordings | 10 speakers (2 each by first language: Chichewa, Tumbuka, Sena, Yao, one more), own phones, mostly budget Android; all speak Chichewa |
| Categories | 11: good, too quiet, far away, background voices, background music, noisy surroundings, wind, echo, too close or muffled, silence in a quiet room, silence with background noise |
| Amount | 36 clips per speaker (3 per category + 3 extra good), about 12 minutes each; about 360 clips, 2 hours |
| Labelling | Recorded clips + about 1 hour of pilot clips (about 540 in total), shuffled and blind, every clip rated by 3 raters |
| Questions | Is someone speaking? Then three 1–5 scales following ITU-T P.835: voice clarity, background, overall quality |
| Usable | Overall mean score of 3 or more (every word can be understood) |
| Design choices | Every speaker records every category (separates condition from speaker and phone, as in CHiME-3); conditions alternate in order between speakers; recording categories are metadata, not labels |

## Plan and first steps

About one working week from recording to labelled data.

- [ ] Read `docs/onboarding-ange.md` and `docs/speech-recordings/quality-assurance.md`, then the recording guide and the labelling plan
- [ ] Settle the open decisions below with Dunstan
- [ ] Prepare a consent form for the speakers, including open publication of their clips
- [ ] Recruit the 10 speakers and 3 raters (raters must not be speakers)
- [ ] Collect each speaker's name, WhatsApp number, first language and phone; Dunstan registers them as test participants
- [ ] Recording day: speakers follow the recording guide (about 1 hour each)
- [ ] Transcribe and score the recorded clips; build the pool and the rating sheet
- [ ] Rater training, then 2–3 days of rating
- [ ] Analysis: rater agreement; which measures predict the ratings (our stored metrics, DNSMOS, Brouhaha); proposed tier-1 and tier-2 audio cut-offs, written up for Dunstan
- [ ] Optional, later: prepare the public release (dataset card, consent check, licence)

## Open decisions

| Question | Current thinking |
| --- | --- |
| Fifth first language for the speakers | Not yet chosen (e.g. Lomwe, Tonga or Ngoni) |
| Pilot share of the pool: 1 or 2 hours | 1 hour suggested (about 5 hours of rating per rater); 2 hours adds about 3 hours each |
| Rating tool | A spreadsheet with clip ID, play link and the four answers; the supervision dashboard could host it later |
| Recording date | Not yet set |
| Public release | Interest confirmed; needs speakers' consent, a licence and a dataset card |

## Technical notes

- **Speakers are test participants** (`is_test = true`, batch `qc-anchors`), so their clips never
  enter the main dataset. The ASR batch and tier-2 scoring skip test participants unless told
  otherwise: run them with `--include-test --participants P00XX,...` (see `docs/platform/deployment.md`).
- **Clip labels:** before each category, speakers send its code (`C1`…`C11`) as a text message. The
  bot stores every message with its time (`inbound_messages`), so each clip's category is the last
  code that speaker sent before it. The paper log is the backup.
- **Silence clips** may be rejected on arrival (`no_speech`) and are then not transcribed; they are
  still stored and go into the rating pool.
- **Stored metrics per clip** (for the analysis): `recordings` (`duration_s`, `active_seconds`,
  `mean_dbfs`, `snr_db`), `qc_results` (word count, words per second, compression ratio, CER),
  `transcripts` (`avg_logprob`).
- **Ready-made measures to compare:** DNSMOS P.835 (predicts the same three scales from audio alone;
  trained on noise-suppressed speech), Brouhaha (speech, SNR and reverberation per frame).

## Sources

- [DNSMOS P.835](https://arxiv.org/pdf/2110.01763)
- [Brouhaha](https://huggingface.co/papers/2210.13248)
- [NISQA corpus and model](https://arxiv.org/pdf/2104.09494v1.pdf)
- [P.808 crowdsourced rating study](https://arxiv.org/pdf/2005.08138)
- [CHiME-3 recording design](https://shadow.merl.com/publications/docs/TR2015-136.pdf)
- [Emilia dataset pipeline (DNSMOS filtering)](https://arxiv.org/pdf/2407.05361)
