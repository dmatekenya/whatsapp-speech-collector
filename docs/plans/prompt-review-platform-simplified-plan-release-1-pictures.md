# Prompt review platform: simplified plan (Release 1: pictures)

Nov 15, 2026 · @Dunstan

## What we are building

A browser screen where annotators review candidate prompts, assign a domain and approve, reject or flag them. Supervisors check a sample. Approved items are exported as a folder that Dunstan imports into the live `prompts` table with the existing importer.

We build it in three releases. **Release 1 is pictures only**, and it must work on its own before we start the others:

1. **Pictures** (this plan): review preloaded images.
2. **Conversation starters** (later): annotators write starters, another person reviews them.
3. **Sentences** (later): review and correct preloaded sentences.

The rest of this plan covers Release 1.

## How it fits the current system

- **Review data lives in PostgreSQL on the droplet**, in new tables separate from the live `prompts` table. The website may cache, but Postgres is the one authoritative copy.
- **The website talks to it through the gateway**, under a new `/v1/prompt-review/` namespace, separate from the audio supervision endpoints.
- **Original images stay on the droplet.** The website shows previews by candidate ID through the gateway, never by file path.
- **Approving does not publish.** Approved items are exported to a folder; Dunstan runs the importer (dry run first) to make them live. The bot never sees an item that has not been through this step.
- **No contributor data is needed.** Prompt review never touches names or phone numbers.

## Release 1 workflow

1. **Register a batch.** Dunstan places finished images in `data/prompts/images/review/<batch>/` and registers the batch with its source category (e.g. `ai-generated`). Re-registering the same batch must not create duplicate candidates.
2. **Check files automatically.** Format (JPG, PNG, WebP), readable, under 50 MB, and exact-duplicate check by file hash against other candidates and live images.
3. **Annotator reviews.** Each annotator gets the next unreviewed image; two annotators never get the same image by accident. They pick a domain and approve, reject or flag.
4. **Supervisor checks.** A random 10% of each annotator's decisions, plus every flagged item. The supervisor confirms or corrects; a correction is a new record, not an edit.
5. **Export approved images.** Once a supervisor has signed off the whole batch, the coordinator selects its approved items; the platform copies the originals to `data/prompts/images/pictures_all/<release_batch>/<domain>/` and writes a CSV listing them.
6. **Import (manual).** Dunstan runs the existing importer on that release folder, dry run first, then the real import.

The importer treats the first subfolder as the domain, so it must be given the release batch folder, never `pictures_all/` or `review/`.

## The review screen

One image per screen, usable on a phone. The annotator sees:

- The image, with zoom to check detail.
- Prefilled source category (read-only; they can flag it if wrong).
- A **domain** dropdown with the 12 manual domains. No free-text domains.
- Three buttons: **Approve**, **Reject** (reason required), **Flag** (note required).
- A save confirmation, so they know the decision was recorded.

An image passes if it has no text, no recognisable real person, is clear at phone size, is culturally suitable, and has enough detail for a 15–30 second description.

Reject reasons: text in image, recognisable real person, poor clarity, unsuitable content, duplicate, implausible generated details, other.

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
| Dunstan / Ange | Supply image batches; write migrations, roles and grants; run imports; review Stewart's changes |
| Stewart | Review screen, gateway endpoints, batch registration, export, and tests |
| Gloria / Evance (coordinators) | Assign work, resolve unclear cases, clear batches for export |
| Supervisors | 10% sample checks, flagged items, feedback to annotators |
| Annotators | Review images and assign domains |

## Decisions needed before building

These are for Dunstan and Ange to settle; Stewart builds against the answers.

- [ ] **Where the screen runs:** a new area in the Hostinger PHP site, or a page served from the droplet. Either way, data stays in Postgres.
- [ ] **Domain keys:** fix the lowercase\_with\_underscores key for each of the 12 domains, and map the 7 pilot image folders (e.g. `lifestyle_and_dressing`) onto them.
- [ ] **Image source categories:** the list annotators will see prefilled (pilot images are still `TBD`).
- [ ] **API contract:** agree the gateway endpoints and fields for batch registration, next item, decision and export before coding starts.

## Release 1 is done when

- [ ] Two annotators can work at the same time without getting the same image.
- [ ] Re-registering a batch or double-clicking Submit creates no duplicates.
- [ ] Rejected and flagged images, and any batch not yet signed off, never reach the export folder.
- [ ] A small test batch goes all the way: review folder → review → supervisor check → export → importer → shown by the bot.
- [ ] The screen works on phone and desktop.
- [ ] Recording supervision and the WhatsApp bot still work as before.

## Not in Release 1

Conversation starters, sentence review, inter-annotator agreement assignments, automated publishing, and payment tracking come later, each as a separate change. Sentence review will also need decisions on numbers in sentences and on the political (`ndale`) starters.
