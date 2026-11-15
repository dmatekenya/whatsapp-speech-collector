# Project scope and design

## Overview

- **What:** an open Chichewa speech dataset (~500 hours) for speech recognition, about 30% read
  sentences and 70% spontaneous speech, recorded by adult volunteers on their own phones.
- **How:** contributors are registered offline with consent, then record voice notes in WhatsApp
  with a bot ("Tiyankhuleni"). The bot sends tasks (read a sentence, describe a picture, answer a
  question, or talk freely), checks each voice note on arrival and tracks progress toward each
  contributor's target in minutes.
- **Where:** the bot, database and audio run on one server (DigitalOcean droplet) and use the Meta
  WhatsApp Cloud API. A fine-tuned Chichewa Whisper model on a rented GPU (Modal) transcribes
  recordings twice a day. Supervisors review clips in a web dashboard.
- **Quality:** every clip passes through up to three tiers of checks (instant checks on arrival,
  automatic checks after transcription, human review) that combine into one label per clip:
  Bronze (not yet validated), Silver (validated automatically), Gold (validated by a human) or
  Rejected (see the recording QA protocol).
- **Privacy:** recordings are linked only to a pseudonymous participant ID; names and phone numbers
  stay in a restricted registry (see the privacy chapter).
- **Status:** pilot from 2026-11-09 (16 contributors × 20 minutes); main collection in weekly runs
  afterwards.

## Purpose and scope

- Goal: an open Chichewa speech dataset of about 500 hours for ASR (and a TTS-ready subset from the
  HQ group), recorded between Months 3 and 8 of the work plan (Sep 2026 – Aug 2027).
- **Speech mix:** about 30% scripted (read) speech and 70% spontaneous speech, following
  Masakhane guidance: roughly 150 hours read and 350 hours spontaneous. Current decision: picture
  descriptions, answers to conversation starters and unprompted free speech all count as
  spontaneous (unscripted, elicited speech); to be confirmed with Masakhane. Spontaneous speech
  needs transcription, while read speech mostly reuses the prompt text, so the transcription
  workload follows the spontaneous share. (The work plan's earlier figure of ~200 hours of read
  speech needs updating to match.)
- Two recording groups: HQ (20 speakers, USB/condenser microphones, recorded separately) and SR
  (smartphone cohorts, by district: 9 cohorts SR-C01–SR-C09, three districts each). This document
  covers the SR channel.
