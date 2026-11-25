# Prompt Methodology

Source: ZaZo AI Ltd (2026), *Chichewa Speech Project: Prompt Generation and Review Manual*, supplied as `prompt-generation-guide-v2.pdf`. Page numbers below refer to the printed PDF pages. These documents adapt the guide; they do not establish that the described annotation platform is already implemented.

## Study design

Prepare a varied, reviewed prompt bank for 500 hours of Chichewa speech: 150 hours scripted and 350 hours spontaneous. The spontaneous component comprises picture descriptions, responses to conversation starters and unprompted free speech, including previously recorded audio. See {doc}`targets </prompt-generation/targets>` for quantities, duration assumptions and domain allocations (guide §§3–5, pp. 3–10).

| Speech type | Task type in the collector | Elicitation |
| --- | --- | --- |
| Scripted | `sentence` | Read a supplied Chichewa sentence aloud. |
| Spontaneous | `image` | Describe a picture in the participant's own words. |
| Spontaneous | `conversation_starter` | Respond freely to an open-ended Chichewa question or invitation. |
| Spontaneous | `free_speech` | Speak on a self-selected topic; no fixed prompt bank. |

Speech type is derived from task type in `config/assignment_settings.yaml`. Sentence text provides a reference for checking readings; spontaneous responses require transcription.

## Preparation and release process

1. Plan batches against category and domain targets. Include several subjects, settings, vocabulary choices and experiences within each domain, rather than minor variations of one topic.
2. AI/NLP leads extract candidate sentences from books, PDFs and online texts and prepare picture banks from image collections or manual sourcing/creation. Annotators primarily create conversation starters. Manual sentence or picture entry is used only for assigned tasks when enabled.
3. Retain source category and available references. Keep picture origin, licence/permission and notes separately from importable image folders. Source information alone does not establish permission to reuse material.
4. Screen candidate banks automatically for format, duplicates and category-specific problems, then load them for human review. Automation identifies problems; it does not approve content.
5. Annotators apply the {doc}`manual </prompt-generation/manual>`, make permitted text corrections, assign one primary domain, check metadata and approve, reject with a reason or flag uncertainty.
6. Independently review newly created conversation starters. Apply supervisor sampling, agreement checks and adjudication described in {doc}`quality assurance </prompt-generation/quality-assurance>`.
7. The coordinator confirms batches for release after unresolved content and permission issues are settled. The data manager prepares and transfers approved prompts into the collection system. Preserve original content and review history.
8. Monitor accepted recording duration, prompt reuse and domain coverage during collection. AI/NLP leads revisit estimates and assignment settings using observed results.

## Extracted reading units

The text pipeline prepares reading units of 15–55 words, using the same inclusive range for
extraction and sentence imports, without a separate target-length flag. Valid paragraphs up to
55 words stay whole. Longer paragraphs are split into complete sentences, then consecutive
passing sentences from the same paragraph are grouped up to 40 words. A failed sentence ends
the current group. Single sentences of 41–55 words remain standalone units; sentences over
55 words are rejected. A final group under 15 words merges into the preceding group only if
the combined unit fits within 55 words; otherwise it is rejected.

OCR preparation rejects blocks containing an original line over 70 characters as likely merged
columns, repairs whole-word `lye` to `Iye`, and removes underscore and stray `=` / `§` separators.
Unit cleaning normalizes repeated internal quote marks to straight apostrophes, trailing
ellipsis runs to one full stop, and unmatched boundary quotes before balance checking.
Remaining quote imbalance, punctuation followed by a lowercase continuation, and other format,
language and duplicate checks can reject units. OCR also checks mixed-case word noise and
unknown words, allowing closing quotes after `. , ? !`. Kept units pass importer validation.

Candidate records retain unit type, sentence count, document ID and block index alongside source
provenance and rejection reasons. Model-suggested domains, confidence, model and scoring time
are saved separately and merged by stable unit ID, so rerunning sentence preparation preserves
existing scores. These suggestions do not replace human review. See the
[text pipeline README](https://github.com/dmatekenya/whatsapp-speech-collector/blob/main/src/prompt_generation/text/README.md) for settings and output schemas.

## Picture content rules

Real people and faces are allowed. Reject or flag pictures showing public figures (politicians or celebrities), political campaign events or party material. Reject or flag sensitive situations: patients receiving treatment, children in distress, funerals or grief, or people shown in a demeaning way. Keep the other criteria: no visible text, clear at phone size, culturally suitable and enough detail for a 15–30-second description. This project decision of 2026-11-15 supersedes the supplied guide’s restriction on real people.

## Classification and metadata

Record category, one primary domain, source category and an available source reference (document title, filename/page or URL). Prefill category and source details for loaded banks; annotators flag doubtful or missing information without inventing references. For manual entry, annotators supply source category and available reference. Picture provenance remains separate from importable folders (guide §3.1).

Choose domains by overall meaning and the central activity or intended response. Growing maize is agriculture; cooking it is food; selling it is work/trade. A child visiting a hospital is health. School-fee budgeting may be finance, while access to schooling is education. Flag unresolved ambiguity, record the competing domains and use coordinator decisions consistently. Do not add domains or count an item under several domains. Domain scopes are in the {doc}`manual </prompt-generation/manual>`.

## Responsibilities

| Role | Responsibility in the guide |
| --- | --- |
| Annotators | Complete orientation/practice; review or create assigned content; record decisions; escalate uncertainty and recurring problems; revise returned work. |
| Prompt Annotation Coordinator: Gloria Chirwa / Evance Mathewe | Assign work, track category/domain progress, resolve escalations, confirm release batches and verify work/payment records. |
| Supervisors (to be confirmed) | Check language, classification and decisions; give feedback, return work and escalate unresolved issues. |
| AI/NLP leads: Dunstan / Ange | Prepare banks, confirm targets and methodological decisions, and approve substantive guideline changes. |
| Data Manager / IT Lead: Stewart Chikomola | Maintain platform/access, preserve content and reviews, and transfer approved prompts. |

Role names reflect the supplied guide (§2), rather than a verified current staffing roster.

## Collection implementation and planning distinctions

The guide defines reuse as accepted recordings by different speakers. Assignments, retakes and rejected recordings are not successful uses. The collector also reserves capacity for pending assignments; reservation is operational capacity, not achieved methodological reuse. Prompts carry over between rounds and participants should not receive the same prompt twice.

The checked-in pilot settings use speaker ceilings of 5 sentences, 7 pictures and 3 conversation-starter responses per prompt, and an audio split of 30%/25%/25%/20%. The guide instead plans with reuse of 3/2/4 and a total-hour split of 30%/21%/35%/14%. These documents adopt the guide for production planning; configuration remains unchanged. Reconcile operational settings before production collection.

Importer IDs use separate sequences: `S00001`, `I0001` and `C0001`. Free speech has no prompt IDs. Import order must remain consistent across environments where matching IDs are required. Technical handover details are in the {doc}`manual </prompt-generation/manual>`.
