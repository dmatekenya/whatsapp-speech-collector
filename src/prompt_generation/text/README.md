# Chichewa text pipeline

Turns WordPress WXR exports and newspaper page PDFs into body text (stage 1), then into
filtered, deduplicated reading units (stage 2), and suggests one domain per kept unit (stage 3).
Sits next to the picture pipeline in `src/prompt_generation/`. Settings are in
`config/sentence-extraction.yaml` and `config/domains.yaml`.

Raw inputs are read only, from `data/chichewa-text/raw-articles-books/`:

- `nation-articles-wordpress-needs-preprocessing.xml` (Nation WXR export)
- `malawi-news/` (newspaper page PDFs, searched recursively)

Other files in that folder are not read.

## System tools

Install poppler and tesseract (used through `subprocess`, never through a shell):

    brew install poppler tesseract

- `pdfinfo`, `pdftotext`, `pdfimages`, `pdftoppm` come from poppler.
- `tesseract` runs OCR with the `eng` language data. Pages whose text layer has fewer than
  `min_text_layer_words` letter-words are OCR'd.

## Usage

Run the stages in order. No path flags are needed; the roots come from config.

    uv run python -m scripts.prompt_generation.extract_sentences extract --run-id <id> [--ocr-workers 4] [-v]
    uv run python -m scripts.prompt_generation.extract_sentences sentences --run-id <id> [-v]
    uv run python -m scripts.prompt_generation.extract_sentences domains --run-id <id> [-v]

`domains` needs `OPENROUTER_API_KEY` in `.env`. Each command prints a summary.

## Outputs

Stage 1, `data/chichewa-text/extracted-text/<run-id>/` (gitignored):

- `documents.jsonl`: one record per WXR post or newspaper page (doc_id, source_category,
  source_reference, method, source_file, page, doc_title, doc_date, date_source, blocks, word_count).
- `texts/<doc_id>.txt`: kept body text, one block per paragraph, for reading by eye.
- `blocks-debug.csv`: every dropped block with its doc_id and reason.
- `run-log.json`: documents per source and method, dropped blocks by reason, skipped pages
  with reasons, failures, OCR pages run and cache hits, and pages with no date.

OCR text is cached in `data/chichewa-text/extracted-text/_ocr-cache/` by the PDF's sha256, so
re-runs only OCR new files.

Stage 2, `data/prompts/text/extracted-sentences/<run-id>/`:

- `sentences-all.csv`: every candidate unit with status and drop_reason.
- `sentences-kept.csv`: kept units in the importer's column format (`text,source,domain,...`).
  `source` is `source_category` (`nation-publications` or `malawi-news`).
- Both CSVs record `unit_type` (`paragraph`, `group` or `sentence`), `n_sentences`, `doc_id`
  and zero-based `block_index` within the document's kept Stage 1 blocks.
- `run-log.json`: counts per source and method, drop reasons, `lexicon_words` and `bank_sentences`.
  The existing `sentences_found` and `sentences_kept` keys count units.

Stage 3 (`domains`) stores results in `domain-scores.csv` with columns `sentence_id`, `domain`,
`domain_confidence`, `domain_source`, `model` and `scored_at` (UTC). It merges the domain fields
into `sentences-kept.csv` by `sentence_id` and adds a `domains` block to `run-log.json`.

Both `sentences` and `domains` merge saved scores, so re-running Stage 2 preserves domains for
unchanged unit IDs. Scores for temporarily absent units remain saved. Legacy scores found only
in the kept CSV are migrated; their unknown model and scoring time are left empty.

## Stage 2: reading units

- One inclusive word range applies to all units: `min_words: 15`, `max_words: 55`.
  Both importer ranges in `config/prompts.yaml` must match. There is no separate target flag.
- A paragraph of at most 55 words is checked and kept whole when valid. Short paragraphs are
  dropped as `too_short`; failed short paragraphs are not salvaged by splitting.
- Longer paragraphs use the existing complete-sentence splitter (including abbreviation and
  initial handling). Consecutive passing sentences are grouped in order up to `group_max_words`
  (40); a failed sentence ends the current group and is logged separately. Short component
  sentences may contribute to a group. No group crosses a paragraph or failed sentence.
- A single 41-55-word sentence is kept alone, even though it exceeds the grouping limit.
  Sentences over 55 words are dropped as `too_long`. A trailing group under 15 words merges
  into the previous group if together they fit in 55 words; otherwise it is `too_short`.
- Repeated quote/apostrophe marks inside words become one straight apostrophe. Trailing runs of
  three or more dots become one full stop. An unmatched boundary quote is removed before the
  balance check; remaining imbalance is dropped.
- Units with sentence-ending punctuation followed by a lowercase word are
  `mid_sentence_break`. Existing bracket, digit, English, junk, duplicate and importer checks
  still apply. The importer's character validation is the last filter.
- OCR additionally rejects lowercase-to-uppercase transitions inside words and other noise,
  but allows closing quotes after `. , ? !`. Its lexicon check still applies only to OCR.

## Stage 3: domains

`domains` sends kept sentences to the model in `config/sentence-extraction.yaml` (`model`,
default `anthropic/claude-haiku-5.5`) through OpenRouter, in batches of `batch_size` (25). The
domain keys, scopes, examples and boundary rules are in `config/domains.yaml`.

- A reply must be a JSON array with one `{"id", "domain", "confidence"}` per sentence. A bad
  reply is retried once. If the retry also fails, those rows get `domain_source = unscored`
  and are tried again on the next run.
- Rows that already have a domain are skipped, so runs can be resumed.
- Spending stops at `max_spend_usd` or `max_calls`. The rows not yet reached stay empty, and
  the summary says so.

## How pages are read

- Text layer first (`pdftotext`, default reading order). OCR is used when it yields fewer than
  100 letter-words.
- OCR uses the single embedded image (`pdfimages -j`) when there is one, otherwise a 300 dpi
  render (`pdftoppm`). Only one-page PDFs are read; multi-page files are skipped and logged.
- Pages containing any `page_skip_keywords` (case-insensitive) are skipped and logged.
- Dates come from the first date of the printed range in the first six lines; otherwise from
  the `YYYY_MM_DD` file name (`date_source` says which). Missing dates do not reject documents.
- OCR blocks containing an original line longer than `ocr_max_line_chars` (70) are dropped
  as `merged_columns`, before lines are joined. Other OCR blocks repair the whole word `lye`
  to `Iye`, replace runs of underscores and stray `=` / `§` with spaces, and collapse whitespace.
  These changes do not affect text-layer pages or WXR articles. Cached OCR stays raw, so
  re-running Stage 1 applies the current cleanup without repeating cached OCR work.

## Tests

    uv run pytest tests/prompt_generation/text -q

Tests use small fixtures in `tests/prompt_generation/text/fixtures/` and fake poppler and
tesseract (`conftest.py`), so they need no network and no system tools.
