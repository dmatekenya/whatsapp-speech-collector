# Picture candidate harvester

Finds openly licensed photos relevant to everyday life in Malawi, filters them and writes a
candidates file for scoring and human review. It never writes to `review/`, `pictures_all/`
or the live `prompts` table.

## Sources

| Source | Status | Account |
|---|---|---|
| `commons` (Wikimedia Commons) | done | none; set `HARVEST_CONTACT_EMAIL` |
| `openverse` | next | none (optional token) |
| `flickr` | later | Flickr Pro + API key |

## Run

```bash
export HARVEST_CONTACT_EMAIL="you@example.org"
uv run python -m scripts.harvest_images --sources commons \
  --domains food,transport --limit-per-query 20 --run-id 2026-11-15-commons-test -v
```

Output in `data/prompts/images/harvest/<run-id>/`:

- `candidates.csv`: every candidate found, kept or dropped, with `drop_reason`
- `thumbs/<candidate_id>.jpg`: 512 px thumbnails of kept candidates
- `run-log.json`: settings and counts per domain and source

API responses are cached in `data/prompts/images/harvest/_cache/`, so a re-run with the
same queries makes no new API calls (`--no-cache` to refresh).

## Pipeline steps

1. Search each domain's queries (`config/prompt-generation.yaml`), metadata only.
2. Drop: non-open or non-commercial licences (unless `allow_non_commercial: true`),
   unwanted file types, maps/logos/scans, images under 800x600, and anything with no
   Malawi signal (geotag inside Malawi, or Malawi place name in categories/title/description).
3. Drop exact duplicates (same source image or same file URL).
4. Download thumbnails, compute file and perceptual hashes, drop near-duplicates
   (keeps the largest original).
5. Optional `--ocr`: flag visible text with EasyOCR (`uv add easyocr` first; slow on CPU).

## Adding a source

Write `sources/<name>.py` with a class that has `name` and
`search(query, domain_key, limit) -> list[Candidate]`, register it in
`sources/__init__.py:build_source`, and add parser tests with a saved JSON response in
`tests/prompt_generation/fixtures/`.
