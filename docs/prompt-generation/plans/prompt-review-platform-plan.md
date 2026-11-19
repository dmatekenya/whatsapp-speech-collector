# Prompt review platform: implementation plan

Prepared for Stewart Chikomola • 15 November 2026  
Project: Tiyankhuleni / Chichewa Speech Project  
Technical review: Dunstan Matekenya and Ange

## 1. Goal and delivery order

Add a browser-based prompt annotation workspace to the existing authenticated website. Deliver it in three independently usable releases:

1. **Pictures:** review preloaded images, assign a domain, approve/reject/flag, and complete supervisor checks.
2. **Conversation starters:** annotators write starters, complete attributes and submit them for independent review.
3. **Sentences:** review preloaded sentences, make permitted corrections, assign domains and submit decisions.

**Picture review must be deployable and usable before the other two categories are implemented.** Build only the shared foundation needed by pictures initially. Add later categories behind separate feature flags and additive migrations. Existing sentences mean sentence review is the lowest initial priority.

This is a development handover, not a request to implement automated image generation. Dunstan/Ange will supply candidate images and sentence batches. Stewart builds the platform, ingestion interface, review workflow and approved-output integration.

## 2. Repository findings and recommended integration

### What exists

Repository inspected on 15 November 2026:

- `main`: `6c1e5b24b97e8d04421daf715415cbcd35331aa7`.
- `supervisor-dashboard`: `b8a25a5ea38b845d2fc057679db840c2ecad21ea`.

The dashboard implementation is on **supervisor-dashboard**, rather than in the inspected main tree:

| Existing component | Implication |
|---|---|
| `supervision_dashboard/`: PHP/MySQL portal, authenticated accounts, review screens and integration code | Reuse the website’s visual shell, login/session handling and interaction patterns. |
| `gateway/gateway.py`: FastAPI gateway between Hostinger and DigitalOcean | Extend the server-to-server pattern for candidate inventory, protected image delivery and review operations. |
| `src/storage/models.py`: live `prompts` table and recording QC models | Keep candidate annotations separate from live prompts and audio QC decisions. |
| `scripts/import_prompts.py`, `src/importers/images.py`, `src/importers/prompts.py` | Reuse the validated import/preparation logic after review, not to populate the review queue. |
| `config/prompts.yaml` | Already defines formats, word-count rules, text normalization and prompt ID sequences. |
| `src/tasks/bank.py` | The bot selects active prompts. New imported prompts default to active; importing a candidate would bypass human review. |

The dashboard README explicitly says its source snapshot is **not a verified export of the running Hostinger instance**. Reconcile the maintained branch with the deployed website before extending it. Some older dashboard documents describe superseded QC endpoints; use current code and the reviewed collector contract, not an older README alone.

### Recommended architecture

- Add a distinct **Prompt annotation** area to the existing PHP website.
- Keep website accounts/sessions in the existing identity system. Add explicit prompt permissions: annotator, supervisor and coordinator; do not give annotators access to contributor contacts or recording supervision.
- **Recommended new ownership rule:** keep the canonical candidate, review and release records in separate PostgreSQL tables on DigitalOcean, accessed through the gateway. Hostinger may cache data, but should not maintain a competing authoritative copy of review state.
- Continue to store original image files on DigitalOcean. The website serves previews through an authenticated proxy/gateway by candidate ID.
- Add dedicated prompt-review credentials and a feature switch for its writes. Do not reuse or broaden the recording-QC writer’s permissions.
- Keep expensive scanning, image processing and publication in background/CLI jobs. A browser request should not scan a whole directory or run an import.
- Treat recording assignment settings and prompt annotation assignment settings separately: different speakers recording one prompt is unrelated to multiple annotators reviewing one prompt.

This architecture is a proposal for the new module, not functionality already present in the repository.

## 3. Candidate input and approved output

### Picture directories

Use Dunstan’s proposed locations, with batches underneath them:

| Location | Purpose |
|---|---|
| `data/prompts/images/review/<input_batch>/` | Incoming original pictures awaiting annotation. Domain subfolders are not required here. |
| `data/prompts/images/pictures_all/<release_batch>/<domain>/` | Approved originals exported for a specific release. Each release batch contains one source group. |
| Configured `PROMPT_MEDIA_ROOT` | System-prepared JPEGs used by the collection bot; separate from original files. |

Example approved file:

`data/prompts/images/pictures_all/round-1-ai-generated/food_cooking_and_nutrition/<candidate_uuid>.jpg`

The importer must receive **the release batch folder**, such as `pictures_all/round-1-ai-generated/`. Passing `pictures_all/` would make `round-1-ai-generated` the domain, because the importer reads the first subfolder as the domain.

The plain folder name **review is not ignored by the current importer**. Add an explicit staging-path guard, including against importing a parent directory that contains staging files. The release job should accept only a coordinator-cleared release manifest. Do not import `data/prompts/images/`, `review/`, or an arbitrary parent folder.

### Ingestion contract for Dunstan’s automation

For each completed batch, provide:

- A unique batch key and source category.
- Original files in the batch directory.
- Available source references and provenance/permission notes.
- Optional automated domain suggestions and validation results, stored separately from human labels.

No annotator-prepared image `metadata.csv` is required. A batch registration command/API can accept these details; automation may optionally supply a machine-generated JSON manifest for per-file references. If one source category covers the whole batch, register it once. Keep source reference optional; require source category before a review can be finalized.

Only ingest finished files. The producer should write temporary files outside the ready set, then atomically rename them or mark the batch ready. Scanning must be repeatable, without generating new candidates or payable work for unchanged inputs.

### Identity, duplicates and movement

- Give each candidate a stable internal UUID. Keep a checksum, original relative path, source metadata and batch identity.
- Preserve the original. **Copy** cleared originals into approved release folders; do not delete/move the only source copy after a click on Approve.
- Check exact file hashes within the candidate pool and against live image prompts. Flag likely visual duplicates where detection is available; human review remains necessary.
- Replacing file contents creates a new version requiring review. A rename alone should not create new payable work.
- Use immutable, checksum-verified release manifests linking candidate/version, approved domain and exported path.
- Keep release batches fixed after publication. Corrections create a tracked revision/release rather than silently changing an already released file.

### Similar arrangement for sentences

Use the repository’s existing text root:

- Candidates: `data/prompts/text/review/<input_batch>.csv`.
- Approved exports: `data/prompts/text/approved/<release_batch>.csv`.

Candidate ingestion preserves original text and supplied source details. Export reviewed text only. Do not recursively import the text root.

For conversation starters, use platform entry as the main input and export approved records under `data/prompts/conversation-starters/approved/<release_batch>.csv`.

## 4. Shared workflow and records

### Assignment and decisions

Use an explicit claim/assignment workflow with expiry and an atomic “next item” operation. Ordinary assignment should avoid two annotators unintentionally receiving the same candidate. Deliberate independent assignments for agreement checks are an exception.

Review decisions are **approve**, **reject** and **flag**. Reject requires a reason; Flag requires a note. A skipped item or expired claim is not a completed review.

Keep these concepts separate:

| Concept | Suggested values |
|---|---|
| Candidate workflow | queued, in_review, flagged, returned, cleared, rejected |
| Assignment | primary review, independent agreement review, supervisor check |
| Publication | not_released, queued, exporting, imported, failed |
| Payment verification | pending, verified, excluded, paid |

A primary approval can clear an unsampled item only after required checks and coordinator release conditions are satisfied. Sampled items, flagged items and agreement disagreements must be resolved before they can clear. A rejected prompt can still represent a correctly completed, payable review.

### Logical records

Implement these as reviewed migrations; final table names can follow repository conventions:

- **Batches:** category, source category/reference defaults, provenance status, ingestion time and validation version.
- **Candidates and versions:** stable ID, category, immutable original text/file reference, current reviewed text, domain, source details, automatic checks and version number.
- **Assignments:** candidate version, annotator, purpose, claim expiry and completion time.
- **Review decisions:** append-only decision, final text/domain snapshot, reason/note, actor and timestamp.
- **Supervisor checks:** sampled review ID, finding, correction/return and resolution. A reversal is a new event, not deletion of history.
- **Release jobs/items:** candidate version, destination, checksum, operation key, importer result and assigned live prompt ID.
- **Payment records:** verified work unit, annotator, rate/version, amount, verification and payment-period reference.

Enforce one completion/payment per assignment, idempotent submissions and version-based conflict checks. A retry with the same operation UUID returns the original result; a stale version returns a conflict requiring refresh.

Independent reviewers must see the **same candidate version** without another reviewer’s edits or labels. Preserve all independent decisions before adjudication.

### Domains and sources

Provide one required domain dropdown with the manual’s 12 domains. No additional mandatory topic field and no free-text domain creation:

1. Family, relationships and community life
2. Food, cooking and nutrition
3. Health, hygiene and wellbeing
4. Agriculture, livestock and fishing
5. Work, trade and personal finance
6. Education and learning
7. Transport and travel
8. Sport, recreation and entertainment
9. Nature, weather and environment
10. Technology and communication
11. Culture, traditions and celebrations
12. Public services and civic life

Maintain stable lowercase underscore keys separately from display labels. Agree the mapping once; the example key in Section 3 is proposed, not an existing standardized importer category. Preserve legacy image-domain mappings explicitly rather than copying pilot spellings into the new taxonomy.

For preloaded records, display source category and available reference as prefilled information. Allow annotators to flag errors; record coordinator corrections in history. Keep source category, source reference, licence/provenance and internal filename as distinct fields.

Hide automated domain suggestions by default. Independent agreement assignments must always be blind to automated suggestions and other annotators’ decisions.

## 5. Releases and acceptance criteria

### Release 1 — Picture review, complete enough to launch

Deliver:

- Login and prompt-specific access controls; annotators see their assigned work and own progress.
- Idempotent batch registration/scanning from `images/review/`.
- Image validation, exact duplicate checks and protected, small previews.
- One-picture screen: zoom/view original detail, domain dropdown, prefilled source details, Approve/Reject/Flag, reason/note and save confirmation.
- No image editing and no requirement for annotators to upload pictures.
- Reject reasons including text in image, public figure or political content, sensitive situation, poor clarity, unsuitable content, duplicate, implausible generated details and other.
- Supervisor queue, correction/return workflow, coordinator resolution and initial agreement assignments.
- Counts by batch/domain/status and verified-work export.
- Approved-folder export plus controlled live import, with retry and reconciliation.

Image review criteria follow the updated manual: no visible text, clear at phone size, culturally suitable and enough detail for a 15–30-second description. Real people and faces are allowed. Reject or flag pictures showing public figures (politicians or celebrities), political campaign events or party material. Reject or flag sensitive situations: patients receiving treatment, children in distress, funerals or grief, or people shown in a demeaning way.

Automated file checks reuse the configured JPG/JPEG/PNG/WebP formats, nonempty/readable image check and 50 MB original limit. OCR and perceptual hashing may help later; they are not prerequisites for the first release and do not replace the annotator.

**Release 1 is ready when:**

- Two annotators can work concurrently without accidental duplicate primary assignments.
- Re-scanning a batch, double-clicking Submit and retrying publication do not duplicate work, payment or live prompts.
- Reject/Flag records cannot enter approved output or the bot.
- Required supervisor checks and disagreement resolution block publication appropriately.
- A small test batch goes from candidate files through review, approved folders and the existing importer into a usable bot image.
- Both later-category flags remain off, and pictures work normally on phone and desktop.
- Existing recording supervision and the WhatsApp webhook still work.

### Release 2 — Conversation-starter creation and review

Enable a separate navigation item with:

1. Assigned domain/subject guidance and a blank Chichewa text box.
2. Source category selection, optional reference and draft save.
3. Word count, configured length validation and normalized-text duplicate checks.
4. Submission to another designated annotator/supervisor.
5. Approve, return for revision, reject or flag; author cannot approve their own starter.
6. Export/publication and approved-creation payment tracking.

Follow the manual’s open-ended, clear, inclusive wording rules. Existing import configuration allows 3–40 words and digits for starters; reuse those rules unless the technical leads approve a change.

An agreement sample means multiple independent reviews of the **same submitted starter**, not asking authors to produce identical text. Keep creator and reviewer attribution distinct.

Release when a creator can submit, revise after feedback and receive approval from another person, and the approved text imports as `conversation_starter`. Picture review must remain available throughout deployment.

### Release 3 — Sentence review

Add candidate CSV ingestion and a sentence screen with original text, editable reviewed text, domain, prefilled source details and decisions.

Reuse the approved manual’s checks: completeness, spelling/punctuation/spacing, natural Chichewa, readability and duplication. Record original/corrected differences. Minor corrections must not invent missing content or change facts.

Existing sentence rules target 8–25 words, with hard limits 3–50. These are word-count checks, not a measured speaking duration. Add language-identification results only as advisory flags; Chichewa or code-switched text should not be rejected solely by an uncertain model.

**Resolve the existing digits-policy conflict before this release:** `config/prompts.yaml` currently rejects digits in sentences, while the manual encourages practical expressions involving quantities, dates, times and money. Dunstan/Ange should decide whether these must be written in words or whether the importer rule changes.

Do not alter already active sentences or historic assignments in place. Re-review existing live sentences through linked candidate records; explicitly approve any replacement/deactivation plan. The current importer treats changed text as a new prompt even when an old prompt ID is supplied.

## 6. QA, reporting and compensation

Implement the manual’s three QA tiers:

1. Automated checks during candidate preparation.
2. Annotator checks following the manual.
3. A random **10% of each annotator’s completed work**, covering each assigned category, checked by a supervisor.

Include rejected reviews in the sampling pool. Review all flags separately, in addition to the sample. Select samples reproducibly and record the sampling period, rate, eligible population and sampled decision IDs. Supervisor returns and expanded checks must be traceable.

Independently assign a configurable subset to at least two annotators for agreement checks. The coordinator sets the overlap proportion before the pilot; do not silently assume another 10%. Initially report domain agreement and approve/reject agreement separately, showing sample counts and how flags were handled. Store disagreement resolution; advanced agreement statistics can follow.

Record the agreed rates:

| Verified work | Rate |
|---|---:|
| Correctly completed picture review, including a justified rejection | MK100 |
| Correctly completed sentence review, including a justified rejection | MK150 |
| Original conversation starter approved after review | MK200 |
| Internet allowance per annotator | MK6,500 |

Corrections/retries on the same work unit do not earn another payment. Supervisor sampling can verify quality, with supervisor batch sign-off for payable quantities; do not require a second full review of every sentence/picture. Store the sign-off explicitly.

The coordinator verifies quantities and handles compensation. Provide a ledger/export, not automated mobile-money payment. The first batch must define how extra agreement reviews and review-only conversation-starter assignments are compensated; no rate has been agreed for those additional tasks.

The base plan of 9,000 sentences, 6,300 pictures and 5,250 starters totals **MK3,030,000**, plus MK6,500 per annotator. Additional candidate reviews, overlap reviews and supervision are outside that base estimate. Internet-allowance frequency and payment schedule should be confirmed before recording repeat allowances.

## 7. Publication and technical interfaces

**Review approval is not a direct insert into live prompts.** Coordinator clearance starts a release job. Use a durable job/outbox and a reconciliation ledger so file copying, database import and status updates can recover from partial failure.

For pictures:

1. Freeze the selected candidate versions and final domains in a release manifest.
2. Group by source; verify required provenance/permission issues are resolved.
3. Copy originals to `pictures_all/<release_batch>/<domain>/` and verify checksums.
4. Run a dry run against that exact batch; resolve errors.
5. Import, record each candidate-to-live-prompt mapping and mark successful items imported. If the importer reports an existing duplicate, reconcile its domain/source/active state rather than reporting a new prompt.
6. Never mark failed items imported; retry with the same release operation key.

The existing importer command pattern is:

```bash
uv run python -m scripts.import_prompts images \
  data/prompts/images/pictures_all/round-1-ai-generated \
  --source ai-generated --dry-run

uv run python -m scripts.import_prompts images \
  data/prompts/images/pictures_all/round-1-ai-generated \
  --source ai-generated --strict --write-ids
```

These commands show current importer syntax, not authorization to run production imports. The new release workflow must enforce approval eligibility. A dry run alone does not prove human approval.

The importer prepares metadata-free RGB JPEGs, maximum long side 1,600 px and maximum prepared size 5 MB, in configured prompt media storage. Never resubmit those prepared copies as originals.

For text, export configured CSV columns including `text`, `source` and final `domain`; use the existing sentence/starter import keys. Preserve source references and review lineage in the platform ledger. The current image importer and CSV configuration do **not** automatically preserve every proposed metadata field: agree explicit mappings/extensions before relying on `prompts.attributes`.

Suggested new gateway namespace, separate from audio supervision: `/v1/prompt-review/`. Provide operations for batch registration, candidate listing/media, atomic claim, draft save, decision submission, supervisor verification and coordinator release. Exact endpoint names are implementation choices.

Use server-derived authenticated identity, role checks, CSRF protection on website forms, protected media access and root-contained file paths. Resolve candidates by ID, not a browser-supplied filesystem path. Do not expose gateway credentials in JavaScript. Conflicts return a refreshable 409; temporary failures can be retried with the original operation key.

Suggested configuration: `config/prompt_review.yaml` for category flags, input/output roots, allowed domains, claim duration, supervisor sample rate, overlap settings and rates. Keep secrets in private deployment settings and document variable names with placeholders only.

## 8. Starting work and handover

1. Reconcile the maintained dashboard branch with the website; agree the integration location and route.
2. Agree the picture ingestion/approval API contract with Dunstan, including stable IDs and source metadata.
3. Add reviewed migrations and prompt-specific permissions. Coordinate migration ownership before deploying.
4. Build picture ingestion, the review screen and supervisor verification with synthetic/licensed test images.
5. Add release/export and verify one complete test batch.
6. Deploy the picture module with its flag enabled; keep starter/sentence features disabled.
7. Start Release 2 and then Release 3 as separate changes.

Deliver each milestone as a reviewable change with setup instructions, migration notes and tests. Follow repository conventions: do not read or overwrite the real `.env`, commit prompt data or credentials, or change production without Dunstan’s review. This handover does not authorize commits, pushes or deployment.

Minimum tests cover permissions, staging exclusion, stable candidate IDs, duplicates, blind assignments, claim expiry, stale edits, submission retries, supervisor sampling, author/reviewer separation, payment uniqueness and publication recovery. Run the applicable PHP checks and Python tests against the reconciled branch, including existing prompt-import and recording-supervision regressions.

### Ownership

| Person/role | Main responsibility |
|---|---|
| Dunstan/Ange — AI/NLP leads | Candidate extraction/generation, initial metadata/checks, methodological decisions and collector integration review |
| Stewart — data manager / IT lead | Website/gateway development, records, access, ingestion and release tooling |
| Gloria Chirwa / Evance Mathewe — prompt annotation coordination | Work allocation, unresolved cases, domain/source decisions, batch clearance and compensation |
| Supervisors | Random checks, feedback, returned work and disagreement resolution |
| Annotators | Picture/sentence review and classification; starter creation and assigned independent reviews |

## Source references

The current Google Docs manual is the authority for annotator procedures and compensation; the repository’s prompt manual is still marked partial.

- [Current prompt generation and review manual](https://docs.google.com/document/d/1PQhEEr0a4XUPA6g8w_sDYn1gnS3QVnu92sP9jt1l33w/edit)
- [Dashboard implementation and snapshot limitations](https://github.com/dmatekenya/whatsapp-speech-collector/blob/b8a25a5ea38b845d2fc057679db840c2ecad21ea/supervision_dashboard/README.md)
- [Website roles and schema](https://github.com/dmatekenya/whatsapp-speech-collector/blob/b8a25a5ea38b845d2fc057679db840c2ecad21ea/supervision_dashboard/app/schema.php)
- [Website integration code](https://github.com/dmatekenya/whatsapp-speech-collector/blob/b8a25a5ea38b845d2fc057679db840c2ecad21ea/supervision_dashboard/app/integration.php)
- [Gateway implementation](https://github.com/dmatekenya/whatsapp-speech-collector/blob/b8a25a5ea38b845d2fc057679db840c2ecad21ea/gateway/gateway.py)
- [Current collector/dashboard contract updates](https://github.com/dmatekenya/whatsapp-speech-collector/blob/6c1e5b24b97e8d04421daf715415cbcd35331aa7/docs/platform/dashboard/updates-2026-11-11.md)
- [Prompt import configuration](https://github.com/dmatekenya/whatsapp-speech-collector/blob/6c1e5b24b97e8d04421daf715415cbcd35331aa7/config/prompts.yaml)
- [Image importer](https://github.com/dmatekenya/whatsapp-speech-collector/blob/6c1e5b24b97e8d04421daf715415cbcd35331aa7/src/importers/images.py)
- [Text importer](https://github.com/dmatekenya/whatsapp-speech-collector/blob/6c1e5b24b97e8d04421daf715415cbcd35331aa7/src/importers/prompts.py)
- [Import CLI](https://github.com/dmatekenya/whatsapp-speech-collector/blob/6c1e5b24b97e8d04421daf715415cbcd35331aa7/scripts/import_prompts.py)
- [Prompt model and active default](https://github.com/dmatekenya/whatsapp-speech-collector/blob/6c1e5b24b97e8d04421daf715415cbcd35331aa7/src/storage/models.py)
- [Bot prompt availability](https://github.com/dmatekenya/whatsapp-speech-collector/blob/6c1e5b24b97e8d04421daf715415cbcd35331aa7/src/tasks/bank.py)
