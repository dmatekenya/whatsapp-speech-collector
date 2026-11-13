# Prompt-generation quality assurance

## Existing requirements

Sentence imports check normalized duplicates, word count and allowed characters. Target length is 8–25 words; hard limits are 3–50. Digits are disallowed. Out-of-target sentences within hard limits are flagged. Sensitive questionnaire topics are excluded.

Conversation starters are open questions eliciting the contributor's own words, in Chichewa for the pilot onwards. Their hard limits are 3–40 words.

Images must contain no text and no recognisable real people. The importer rejects unreadable, empty or oversized sources and detects exact byte duplicates. Metadata stripping and resizing are automatic; neither substitutes for content review.

The coordinator runs a dry run, checks warnings and theme spelling, verifies provenance, and resolves rejected items before release. `--strict` prevents partial imports when any row is invalid.

## Pending approval

The dedicated prompt QA source document is not yet available. Reviewer assignments, language-review rubric, sampling rates, adjudication and sign-off procedures remain to be supplied. Do not infer approved procedures from this stub.
