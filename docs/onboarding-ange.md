# Onboarding

Last updated: 2026-11-11. For new team members (first written for Ange).

The project collects about 500 hours of Chichewa speech through a WhatsApp bot ("Tiyankhuleni") to
build an open speech-recognition dataset, funded through Masakhane. A 3-day pilot with 16
contributors ran from 9 November 2026; the main collection follows in weekly rounds. This page
explains the whole project and what to read. The audio-quality experiments have their own handover:
[audio-quality/handover.md](audio-quality/handover.md).

## Reading list, in order

All in this repo, branch `main`. About a day of reading.

| # | Read | Why |
| --- | --- | --- |
| 1 | `README.md` and `CLAUDE.md` | What the repo is, and the working rules (also followed by Claude Code) |
| 2 | `docs/methodology.md`: Overview, then sections 1–7 | The design: participants, WhatsApp channel, task types, sessions, targets, audio handling |
| 3 | `docs/methodology.md` section 8 | The quality-control protocol: three tiers and the final label (Bronze, Silver, Gold, Rejected) |
| 4 | `docs/methodology.md` section 10 (decision log) | Why things are the way they are, in date order |
| 5 | `docs/architecture/database.md` | Every table and column, and how they connect |
| 6 | `docs/deploy.md` | How the server is run: imports, deploys, the ASR batch, tier-2 scoring |
| 7 | `docs/plans/supervision-dashboard-plan.md` and `docs/plans/supervision-qc-contract.md` | The supervisors' review dashboard (built by Stewart) and how it writes decisions |
| 8 | `docs/plans/meta-cloud-api-migration-plan.md` | Optional: why and how we moved from Twilio to Meta's WhatsApp Cloud API |

Code to skim after the docs: `src/conversation/` (the bot), `src/asr/` and
`scripts/modal_asr_app.py` (transcription), `src/qc/` with `config/qc_tier2.yaml` and
`scripts/score_tier2.py` (tier-2 scoring).

## How we collect data with WhatsApp

Contributors record voice notes on their own phones, in WhatsApp, which most people in Malawi
already use daily on cheap data bundles.

1. **Registration (offline):** adults are recruited and give consent through a form; the team
   imports them from a CSV. Each gets a pseudonymous ID (`P0008`…); names and phone numbers stay in
   a restricted registry. They receive a WhatsApp welcome with their target in minutes (20 in the
   pilot).
2. **Sessions:** the contributor sends "Hi" to the bot's number. The bot offers a task type and
   sends up to 10 prompts per session. Four task types: **read a sentence** (scripted, about 30% of
   minutes), **describe a picture**, **answer a question** (conversation starter) and **talk
   freely** (spontaneous, about 70%).
3. **Recording:** each answer is a WhatsApp voice note, 5 seconds to 45–60 seconds depending on the
   task (up to 2 minutes for older voice notes in free speech). Too short, too long or silent clips
   get a retake request.
4. **Progress:** the bot counts recorded minutes per task type against the target; contributors can
   type "progress" at any time.
5. **Behind the scenes:** every voice note is stored (original plus a 16 kHz WAV) and checked on
   arrival (tier 1); twice a day a fine-tuned Chichewa Whisper model transcribes new clips on a
   rented GPU and the scoring compares sentence readings with their prompt (tier 2); supervisors
   review flagged and sampled clips in a dashboard (tier 3).

## How the system fits together

| Part | What it is | Where |
| --- | --- | --- |
| Bot and API | Python (FastAPI) app that receives WhatsApp messages, runs the conversation and stores audio | DigitalOcean server (`bot.zazoai.com`), Docker Compose |
| WhatsApp | Meta WhatsApp Cloud API; contributors message +1 618-602-0282 | Meta |
| Database | PostgreSQL: participants, prompts, sessions, recordings, transcripts, QC results | Same server |
| Transcription | Fine-tuned Whisper (`whisper_b`) run twice a day on a GPU | Modal (called from the server) |
| Tier-2 scoring | CER of sentence readings against their prompt, plus stored audio metrics | Same server, after each transcription batch |
| Supervision dashboard | Supervisors listen to clips and accept or reject them | Hostinger (PHP), reading from the server through a token-protected gateway |

**People:** Dunstan (project lead, owns the design and the server), Stewart Chikomola (IT,
supervision dashboard and gateway), supervisors (review clips), Ange (audio-quality experiments,
then NLP/AI work on the data).

## The audio-quality experiments

The automatic checks can tell whether a sentence was read correctly, but not yet whether a recording
is noisy, distant or muffled. The audio-quality experiments build a labelled audio-quality set
(staged recordings plus pilot clips, rated by three people) and use it to calibrate those checks.
See [audio-quality/handover.md](audio-quality/handover.md), which links to the recording guide and
the labelling plan.

## Rules for working with the data and the repo

- **Personal data stays restricted.** Names and phone numbers live only in the server's registry;
  never put them in git, email, WhatsApp, spreadsheets or chats. Work with participant IDs.
- **Voices are personal data too.** Copies of audio or database backups on a laptop are for a
  specific task only, on an encrypted disk, and deleted afterwards.
- **Secrets:** never read, print or commit `.env` or any token.
- **Code changes go through a branch and review;** nothing is changed directly on the server
  without Dunstan's approval.
- **Design changes are written down:** anything that changes how data is collected or checked
  updates `docs/methodology.md` and its decision log.
- **Test participants** (`is_test`) are excluded from the dataset; use them for any testing on the
  live bot.
