# Prompt review platform: simplified plan (Release 1: pictures)

Nov 15, 2026 · @Dunstan

## What we are building

A browser screen where annotators label a sample of candidate prompts: whether each is usable, its domain and its topic. About 10% of pictures are labelled by two annotators, and disagreements are settled by a supervisor or coordinator. The labels are exported as a CSV that Dunstan uses to build the release folders he imports into the live `prompts` table with the existing importer.

We build it in three releases. **Release 1 is pictures only**, and it must work on its own before we start the others:

1. **Pictures** (this plan): review preloaded images.
2. **Conversation starters** (later): annotators write starters, another person reviews them.
3. **Sentences** (later): review and correct preloaded sentences.

The rest of this plan covers Release 1.

## How it fits the current system

- **Review data lives in PostgreSQL on the droplet**, in new tables separate from the live `prompts` table. The website may cache, but Postgres is the one authoritative copy.
- **The website talks to it through the gateway**, under a new `/v1/prompt-review/` namespace, separate from the audio supervision endpoints.
- **Original images stay on the droplet.** The website shows previews by candidate ID through the gateway, never by file path.
- **Approving does not publish.** Labels are exported as a CSV; Dunstan builds release folders and runs the importer (dry run first) to make pictures live. The bot never sees a picture that has not been through this step.
- **No contributor data is needed.** Prompt review never touches names or phone numbers.

## Release 1 workflow

1. **Register a batch.** Dunstan places a batch in `data/prompts/images/review/<batch>/`: images named `<candidate_id>.jpg` (longest side at most 1,280 px), a `manifest.csv`, and a `READY` file written last. The platform registers the batch from `manifest.csv` once `READY` exists. The `candidate_id` is the picture's ID everywhere, from harvest to export. Re-registering the same batch must not create duplicates.
2. **Check files automatically.** Readable JPEG, and an exact-duplicate check by file hash against other candidates and live images.
3. **Annotators label.** Each annotator gets the next unlabelled picture. About 10% of pictures (the `overlap_rate` setting) are chosen at random for **overlap**: each goes to exactly two different annotators, and neither sees the other's label. Every other picture goes to one annotator only, never two by accident. Labelling has three steps: usable, domain, topic (see the review screen below).
4. **Disagreements and checks.** When the two labels on an overlap picture disagree (on usable, domain or topic), the picture goes to a supervisor or coordinator, who records the final decision. Every flagged item also goes to them. In the first days only, a supervisor spot-checks 5% of each annotator's first 100 labels, to catch shared misreadings of the rules early. Decisions and corrections are new records, never edits.
5. **Export labels.** A command or button writes a snapshot of all saved labels to `review/<batch>/labels/labels-<YYYY-MM-DD>.csv`. It can be run at any time, not only at the end, so model work can start on early labels.
6. **Release (Dunstan).** Dunstan builds the release folders for the existing importer from the exported labels and runs the import (dry run first). The platform does not copy images into `pictures_all/`.

Batches are a **sample**, not every picture: the first batch is about 3,000 pictures drawn from about 16,000 candidates. A model, trained or prompted with these human labels, assigns domains to the rest; that happens in Dunstan's pipeline, outside the platform.

## Batch and label formats

**`manifest.csv`** (one row per picture, written by Dunstan):

| Column | Example |
| --- | --- |
| `candidate_id` | `3f2a9c1e5b7d4e8a`, `ds-5d4be6c8cf0b3a0f3f3444d4` |
| `file_name` | `3f2a9c1e5b7d4e8a.jpg` |
| `source`, `source_category` | `flickr`, `commons`, `openverse`, `dollar-street`, `ai-generated` |
| `licence`, `licence_url`, `creator`, `source_url` | attribution details, shown read-only |
| `title`, `query_domain`, `country`, `region_signal` | context for Dunstan; **not shown to annotators** |

**`labels-<date>.csv`** (one row per label, written by the platform). An overlap picture has two annotator rows, plus an adjudication row when they disagreed:

| Column | Values |
| --- | --- |
| `candidate_id` | from the manifest |
| `usable` | `yes` / `no` |
| `reject_reason` | when not usable: `not-relevant`, `poor-quality`, `visible-text`, `sensitive`, `duplicate`, `other` |
| `reject_note` | free text, when the reason is `other` |
| `domain` | a domain key, or `none-fits` |
| `topic` | a topic key, or `other` |
| `topic_other_text` | free text, when the topic is `other` |
| `flagged`, `flag_note` | escalations |
| `annotator` | pseudonymous annotator ID, never a name |
| `overlap` | `yes` if the picture was sent to two annotators, else `no` |
| `label_role` | `annotator`, or `adjudication` for the supervisor's or coordinator's final decision on a disagreement (the `annotator` column then holds their ID) |
| `labelled_at` | timestamp |
| `supervisor_status` | `not-checked`, `confirmed` or `corrected`, from the early spot checks only |

## The review screen

One picture per screen, usable on a phone, with zoom. The source category and attribution are shown read-only; the annotator can flag them if wrong. Labelling has three steps:

1. **Usable?** Yes or No. If No, a reason is required: not relevant, poor quality, visible text, sensitive, duplicate, or other (with a note). The label is then saved; no domain or topic is asked.
2. **Domain:** a dropdown of the 12 domains, plus **No domain fits**. Choosing No domain fits saves the label and excludes the picture; the choice is recorded.
3. **Topic:** a second dropdown showing only the chosen domain's topics, plus **Other (describe)** with a short text box, required when Other is chosen.

Then save, with a confirmation that the label was recorded. A **Flag** button (note required) is available at every step for cases to escalate.

The domain and topic lists come from `config/domains.yaml` in the repo. The screen shows labels and stores keys (e.g. `work-trade`, `market-selling`). The topic list is still being agreed and will change, so read it from config; never hard-code it. Prefilled answers are never shown: no suggested domain or topic.

**Content rules.** Real people and faces are allowed, including public figures. Not usable: visible text a speaker might read out, unclear at phone size, too little to describe for 15–30 seconds, or not relevant to everyday life in Malawi or nearby countries. Sensitive: patients receiving treatment, children in distress, funerals or grief, people shown in a demeaning way, and graphic or sexual content. Political campaign material is flagged until the project decides on political content.

No image editing and no uploading by annotators.

## Technical rules

These come from what we learned building the supervision dashboard.

1. **Every database change goes through an Alembic migration** in the repo: tables, views, roles and grants. Nothing created by hand on production.
2. **A new writer role for prompt review**, INSERT-only on its decisions table. Do not reuse or widen the QC roles.
3. **Decisions are append-only.** Each carries an operation UUID, so a double-click or retry does not create a second record. A stale screen gets HTTP 409 and must refresh. Copy the pattern from `qc_human_reviews`.
4. **Writes are off by default** behind a flag, and switched on only after an end-to-end test on a restored copy of the database.
5. **All production changes through a reviewed branch**, including containers and the Caddyfile. No test containers on production.
6. **Never import from the review folder.** The importer only ever receives an approved release folder.

## Who does what

| Person | Responsibility |
| --- | --- |
| Dunstan / Ange | Build labelling batches (images, manifest, READY); maintain `config/domains.yaml`; write migrations, roles and grants; build release folders and run imports; review Stewart's changes |
| Stewart | Review screen, gateway endpoints, batch registration, labels export, and tests |
| Gloria / Evance (coordinators) | Assign work, resolve unclear cases, review *Other* topic notes |
| Supervisors | Settle disagreements on overlap pictures, handle flagged items, early spot checks, feedback to annotators |
| Annotators | Decide whether pictures are usable; choose domain and topic |

## Decisions needed before building

These are for Dunstan and Ange to settle; Stewart builds against the answers.

- [x] **Domain keys:** lowercase with hyphens, from `config/domains.yaml` (e.g. `work-trade`, `public-services`). Dunstan maps the 7 pilot image folders.
- [x] **Source categories:** `commons`, `openverse`, `flickr`, `dollar-street`, `ai-generated`, given per picture in the manifest.
- [x] **Labelling flow:** usable → domain → topic, as described above.
- [ ] **Topic list:** being agreed by Dunstan and Ange; it will arrive in `config/domains.yaml`.
- [ ] **Where the screen runs:** a new area in the Hostinger PHP site, or a page served from the droplet. Either way, data stays in Postgres.
- [ ] **API contract:** gateway endpoints for batch registration, next picture, saving a label and the labels export, using the formats above.
- [ ] **Overlap rate:** 10% of pictures go blind to two annotators, replacing a random supervisor sample. A setting, so it can change per batch.

## Release 1 is done when

- [ ] Two annotators can work at the same time without getting the same picture.
- [ ] About 10% of pictures reach two different annotators, blind; when their labels disagree, the picture reaches a supervisor or coordinator, and their decision appears in the export as an adjudication row.
- [ ] Re-registering a batch or double-clicking Save creates no duplicates.
- [ ] The labels export can be run at any time and contains every saved label with its `candidate_id`.
- [ ] A test batch goes all the way: review folder → labelling (with overlap) → adjudication of a disagreement → labels export → Dunstan's import → shown by the bot.
- [ ] The screen works on phone and desktop.
- [ ] Recording supervision and the WhatsApp bot still work as before.

## Not in Release 1

Conversation starters, sentence review, automated publishing, payment tracking and model-based labelling of unreviewed pictures come later, each as a separate change. Sentences and conversation starters will use the same domain and topic list. Sentence review will also need decisions on numbers in sentences and on the political (`ndale`) starters.
