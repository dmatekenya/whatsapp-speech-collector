# Prompt Quality Assurance

Adapted from ZaZo AI Ltd (2026), *Chichewa Speech Project: Prompt Generation and Review Manual*, §7 (pp. 13–14), with existing importer checks identified separately. This is prompt QA; accepted recording quality is a separate collection step.

## Three stages of quality assurance

| Stage | Responsibility | Checks |
| --- | --- | --- |
| Automated preparation | AI/NLP leads and data manager | Sentences: agreed word range, likely non-Chichewa text, duplicates and extraction problems. Pictures: readability, supported formats and exact duplicates. |
| Annotation | Prompt annotators | Apply category-specific {doc}`manual </prompt-generation/manual>` checks, make permitted corrections, assign one domain and check source attributes. Approve suitable items, reject with a reason and flag uncertainty. |
| Supervisor review | Supervisors | Randomly review 10% of each annotator's completed work, covering every assigned category. Check content quality, corrections, domains and decisions. Review flagged cases separately, in addition to the random sample. |

Automation flags potential problems and does not replace human review. The guide does not establish that all proposed automated checks already exist in the collector.

For recurring errors, supervisors provide feedback, return affected work for correction and expand review where necessary. Escalate unresolved cases to the coordinator. Keep prompts with unresolved quality issues outside the collection set.

## Independent review and agreement

Assign a subset of shared prompts independently to two or more annotators. Hide other annotators' labels and any automatically suggested domain until each assessment is submitted. Compare domain labels and approval/rejection decisions. For conversation starters, assess agreement on reviewing the same existing starter, rather than asking annotators to create identical starters.

Supervisors examine and resolve disagreements using the manual, escalating uncertainty to the coordinator. Use recurring disagreements to improve examples, guidance and training. The guide specifies no agreement sample size, statistic or acceptance threshold; these remain operational decisions to define before evaluation.

New conversation starters also require review by another designated annotator or the coordinator before approval, independently of supervisor sampling.

## Review records and release

Retain original content, corrections, domain assignments, review decisions, notes and annotator identities. Also retain supervisor findings and final resolutions. The coordinator shares recurring issues and agreed decisions, confirms release batches and resolves source/permission questions before release. Prefilled source information alone is not proof of reuse permission.

## Existing importer checks and handover verification

`config/prompts.yaml` defines sentence target length of 8–25 words, hard limits of 3–50, no digits, character validation and normalized duplicate detection. In-range but out-of-target sentences receive length flags. Conversation starters have hard limits of 3–40 words. These checks do not establish idiomatic Chichewa, acceptable content or response quality.

Image imports check readable, nonempty sources, supported JPG/JPEG/PNG/WebP formats, a 50 MB source limit and exact byte duplicates. Automatic resizing and metadata removal do not replace checks for visible text, public figures or political content, sensitive situations, near-duplicate scenes or generated distortions. Real people and faces are allowed. Reject or flag pictures showing public figures (politicians or celebrities), political campaign events or party material. Reject or flag sensitive situations: patients receiving treatment, children in distress, funerals or grief, or people shown in a demeaning way. Keep the other picture criteria: no visible text, clear at phone size, culturally suitable and enough detail for a 15–30-second description.

Before importing, run a dry run, inspect warnings, verify domain spelling/completeness and provenance, and resolve invalid items. `--strict` prevents partial imports when any row is invalid. Preserve review/provenance records alongside the released batch; keep non-image provenance outside importable image folders. See the {doc}`manual </prompt-generation/manual>` for file layout and export constraints.

## Details still to define

The guide leaves supervisor identities, the agreement sampling/measurement protocol, expanded-review thresholds, and a complete acceptable/unacceptable sentence and picture example bank unspecified. Resolve these through the coordinator and AI/NLP leads; do not present invented thresholds or examples as approved guide content.
