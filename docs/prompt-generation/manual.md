# Guide for Annotators

Adapted from ZaZo AI Ltd (2026), *Chichewa Speech Project: Prompt Generation and Review Manual*, supplied as `prompt-generation-guide-v2.pdf`, §§2–6 (pp. 1–13). Platform workflows below describe the guide's intended process. The technical handover section documents existing importer behavior.

## General review procedure

Complete orientation and practice reviews before assigned work. Use your own platform account, open the allocated task and follow the category checks below. Review the whole item; automated preparation is not evidence that it is suitable.

- **Approve:** content and attributes meet the criteria, including after permitted corrections.
- **Reject:** the item is unsuitable; record a specific reason.
- **Flag:** an issue needs a decision; explain the uncertainty and escalate to the coordinator.

Assign one primary domain. Check prefilled category, source category and available source reference. Source category is mandatory; a reference may be unavailable. Flag missing or doubtful information without inventing it. Manual entry requires a source category and any available reference. The coordinator resolves permission questions before release. Confirm every review is saved before moving on.

Prompts should be self-contained, clear, relevant to Chichewa speakers and varied in subject, vocabulary and setting. Avoid required personal identifiers, sensitive disclosures and assumptions that everyone shares the same family structure or lifestyle.

## Primary domains

Use the main activity or intended response, rather than isolated words or background details. See [methodology](methodology.md) for classification examples and [targets](targets.md) for allocations (guide Table 2, pp. 4–5).

| Domain | Scope |
| --- | --- |
| Family, relationships and community life | Household routines, caregiving, friendships and community cooperation. |
| Education and learning | School, teaching, vocational training and learning outside the classroom. |
| Work, trade and personal finance | Occupations, markets, small businesses, purchases and saving. |
| Health, hygiene and wellbeing | Everyday wellbeing, exercise, sanitation and access to care. |
| Food, cooking and nutrition | Ingredients, cooking, meals, preservation and eating customs. |
| Agriculture, livestock and fishing | Cultivation, harvesting, storage, animal husbandry and fishing. |
| Transport and travel | Journeys, walking, bicycles, public transport, roads and directions. |
| Nature, weather and environment | Seasons, rainfall, landscapes, wildlife, water and waste. |
| Culture, traditions and celebrations | Customs, clothing, ceremonies, language practices and oral traditions. |
| Sport, recreation and entertainment | Games, hobbies, music, dance, sport and media. |
| Technology and communication | Phones, messaging, internet use, radio and digital services. |
| Public services and civic life | Community facilities, public information, services and local decisions. |

If several domains fit, select the main focus. Flag unresolved ambiguity rather than creating another domain or independently assigning multiple labels.

## Sentences

Review completeness, spelling, punctuation, spacing, grammar and natural Chichewa. A sentence must make sense on its own, fit the recording limits in [targets](targets.md) and avoid duplication. Correct obvious spelling, punctuation, spacing or extraction errors without changing meaning; consult the source for unclear extracted wording where available. Leave correct text unchanged. Reject unusable sentences with a reason and flag uncertainty. Assign the domain, check metadata and approve only the final corrected version (guide §6.1).

The importer currently targets 8–25 words with hard limits of 3–50; out-of-target sentences within hard limits are flagged. Digits are disallowed. These technical checks do not establish language quality or reading duration.

## Pictures

The picture rules below incorporate the project decision of 2026-11-15, superseding the supplied guide’s restriction on real people.

Inspect the entire image, including the background. Reject visible text in the image. Real people and faces are allowed. Reject or flag pictures showing public figures (politicians or celebrities), political campaign events or party material. Reject or flag sensitive situations: patients receiving treatment, children in distress, funerals or grief, or people shown in a demeaning way. Check clarity at phone-screen size, culturally suitable and respectful content, enough detail for a 15–30-second description, and duplicates or near-identical scenes. For AI-generated pictures, inspect distortions and implausible details. Annotators do not edit images. Assign the domain by the central visible activity, check source attributes and submit the decision (guide §6.2).

Example scenes in the guide include buying tomatoes at a market (work/trade), planting maize (agriculture), preparing nsima (food) and collecting water at a borehole (public services). The guide's sentence and picture example placeholders do not supply a complete acceptable/unacceptable example bank.

## Conversation starters

Create starters in natural Chichewa within the assigned domain, varying subjects and situations. Invite a description, explanation, experience, comparison, story or opinion. Keep one main focus; add a short follow-up cue only when helpful. Avoid yes/no questions, quizzes, specialist knowledge, sensitive disclosures and assumptions about family or lifestyle. Allow typical or imagined situations where appropriate. Check for starters inviting essentially the same response (guide §6.3).

Complete domain, mandatory source category and available reference. Use the agreed original-contribution source category for original work. Submit to another designated annotator or the coordinator for review: creation does not approve a starter for collection. The reviewer may approve, return for revision, reject with a reason or flag an unresolved issue.

The guide's English examples illustrate the intended question design; production starters must be written in Chichewa:

| Weak starter | Improved starter | Purpose |
| --- | --- | --- |
| Do you like nsima? | Describe a meal you enjoy and explain why you like it. | Invite more than yes/no. |
| Talk about farming. | Describe how farmers in your area prepare their fields before planting. | Give a clear focus. |
| What does your husband do for work? | Describe a common way people in your area earn a living. | Avoid a family-status assumption. |
| Give the name and address of your nearest neighbour. | Describe how neighbours help one another in your community. | Avoid personal identifiers. |
| What is the scientific cause of malaria? | Describe common ways people in your community protect themselves from mosquitoes. | Invite everyday knowledge. |

## Work verification and compensation

The guide (§2.3, pp. 2–3) specifies MK150 per correctly completed sentence review, MK100 per picture review and MK200 per original conversation starter approved after review. Sentence and picture reviews include justified rejections. Corrections to the same item are not additional payable work. Each annotator receives an internet allowance of MK6,500. The coordinator verifies quantities against supervisor-confirmed platform records and communicates the payment schedule before work begins.

## Technical handover to the collector

Only release reviewed content after the [QA process](quality-assurance.md). Domain folder names below illustrate lowercase underscore formatting; keep one agreed mapping to the domain list. These importer rules come from `config/prompts.yaml` and the existing handover documentation, rather than the PDF.

## Image handover

```text
images/
  round-1/
    family_relationships_and_community_life/
      market_scene_001.jpg
    nature_weather_and_environment/
      lakeside_scene_001.png
```

Store batches under `data/prompts/images/<batch>/<theme>/`. The batch is the selected folder's name; the first subfolder determines the theme (`domain`). Use consistent lowercase names with underscores. Put every image in a theme folder; top-level files import without a theme. Deeper folders do not add theme levels.

Accepted formats: JPG, JPEG, PNG and WebP; source limit: 50 MB. Descriptive filenames are recommended, but no naming pattern is enforced. The importer assigns `I0001`-style IDs. Files and folders beginning with `_` or `.` are skipped.

One source label applies to each import (`ai-generated`, `own-photos`, or a licence/source label), so separate batches by provenance when needed. Provide provenance and permission details separately; these are not read automatically from a manifest. Settle unresolved provenance before dataset release.

Avoid duplicate and near-duplicate pictures; byte hashing catches identical files, not all visual duplicates. Prepared images are generated automatically as metadata-free RGB JPEGs, maximum long side 1,600 px and maximum size 5 MB, under `_prepared/<theme>/I0001.jpg`. Submit originals, not prepared copies. `--write-ids` writes a companion CSV mapping files to prompt IDs.

## Text handover

Sentence CSVs live under `data/prompts/text/`; conversation-starter CSVs under `data/prompts/conversation-starters/`. Both require `text` and `source`; `domain` is optional. Use a new file per batch. Exact column validation is defined in `config/prompts.yaml`.


The importer may accept a missing domain even though the review methodology requires one. Check domain completeness explicitly before release. Review-platform source references and decisions are not automatically preserved by the minimal text CSV schema; retain the review/provenance records separately.
