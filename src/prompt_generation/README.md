# Picture candidate harvester

Finds openly licensed photos relevant to everyday life in Malawi, filters them and writes a
candidates file for scoring and human review. It never writes to `review/`, `pictures_all/`
or the live `prompts` table.

## Sources

| Source | Status | Account |
|---|---|---|
| `commons` (Wikimedia Commons) | done | none; set `HARVEST_CONTACT_EMAIL` |
| `openverse` | done | none; optional `OPENVERSE_TOKEN` |
| `flickr` | done | set `FLICKR_API_KEY` |

## Run

```bash
export HARVEST_CONTACT_EMAIL="you@example.org"
uv run python -m scripts.prompt_generation.harvest_images --sources commons \
  --domains food,transport --limit-per-query 20 --run-id 2026-11-15-commons-test -v
```

Output in `data/prompts/images/harvest/<run-id>/`:

- `candidates.csv`: every candidate found, kept or dropped, with `drop_reason`
- `thumbs/<candidate_id>.jpg`: 512 px thumbnails of kept candidates
- `run-log.json`: settings and counts per domain and source

API responses are cached in `data/prompts/images/harvest/_cache/`, so a re-run with the
same queries makes no new API calls (`--no-cache` to refresh).

Use `--sources commons,openverse` to search both sources. Openverse excludes Wikimedia
results because Commons is queried directly, and requests commercial-use licences unless
`allow_non_commercial` is enabled. Anonymous pages contain up to 20 results; authenticated
pages up to 50. Set `OPENVERSE_TOKEN` in the environment for authenticated requests. It is
sent only to the Openverse search endpoint, never to thumbnail hosts or the shared session.
See the [API reference](https://api.openverse.org/v1/) and
[documented pagination limits](https://docs.openverse.org/projects/proposals/proxy_frontend_api_requests/20241202-implementation_plan_proxy_frontend_api_requests_ip.html).

## Pipeline steps

1. Search each domain's queries (`config/prompt-generation.yaml`), metadata only.
2. Drop: non-open or non-commercial licences (unless `allow_non_commercial: true`),
   unwanted file types, maps/logos/scans, images under 800x600, and anything without the
   signal its domain's `location` requires:
   - `malawi` (default): a Malawi signal (geotag inside Malawi, or Malawi place name in
     categories/title/description).
   - `africa`: a Malawi signal or an Africa signal (geotag inside `africa_bbox`, or an
     African country name, demonym, "africa" or "african" in categories/title/description).
   - `any`: no requirement.
   The `region_signal` column records `malawi`, `africa` or `none`; a Malawi signal takes
   precedence over an Africa signal.
3. Drop exact duplicates (same source image or same file URL).
4. Cap each creator at `max_per_creator_day` photos per day (default 3), then
   `max_per_creator_domain` per domain (default 20), within each source, keeping search order.
   Empty creators are not capped; dates shorter than 10 characters skip the day cap.
   A non-positive setting disables that cap. Excess photos are dropped with `event_cap`
   or `creator_cap` before downloading.
5. Download thumbnails, compute file and perceptual hashes, drop near-duplicates
   (keeps the largest original).
6. Optional `--ocr`: flag visible text with EasyOCR (`uv add easyocr` first; slow on CPU).

## Flickr modes

Select Flickr with `--sources flickr` (or combine it with other sources). Set
`FLICKR_API_KEY` in the environment. Both modes are enabled by default:

```yaml
flickr:
  modes: [geo-sweep, text]
  api_key_env: FLICKR_API_KEY
```

- `geo-sweep` runs once per harvest, independent of the selected domains, queries and
  `--limit-per-query`. It searches the Malawi bounding box with `has_geo=1`, no text,
  `media=photos`, `content_type=1`, `sort=date-posted-desc` and `per_page=250`.
  It returns every matching candidate as `query_domain=unassigned`. Oversized searches
  are split into non-overlapping inclusive upload-date intervals until each has at most
  4,000 results; newer intervals are retrieved first. The upper upload date is the start of the current UTC day, so same-day reruns
  reuse cached search pages. Empty or short pages end each range even when Flickr
  estimates a larger total. If a single upload second still exceeds 4,000, the sweep fails explicitly
  rather than silently returning incomplete results; harvest logs the sweep failure and
  continues with the other searches.
- `text` runs for each selected domain query, preserving the query text exactly, without
  a bbox or `has_geo` constraint. `--limit-per-query` applies. Malawi relevance is assessed
  from returned metadata through the common geo/tag/title/description checks.

Both modes use the same licence, media and extras settings. Allowed licence IDs/names
are loaded through the cached `flickr.photos.licenses.getInfo` endpoint and the existing
licence policy, including `allow_non_commercial`. The photo owner name identifies creators
for diversity caps, falling back to the owner ID when the name is empty. Zero or missing
coordinates are treated as absent and exported as empty CSV cells. All candidates still pass through the usual metadata, diversity and
thumbnail checks; a sweep is not capped by domain quotas.

`unassigned` candidates have no quota target (`null` in `run-log.json`, `-` in the terminal
summary). They skip domain lookup but must have coordinates inside the Malawi box;
kept sweep candidates therefore have `malawi_signal=geo`. The picture scorer can suggest
a domain afterwards. The initial CSV remains `unassigned` until reviewed/reclassified.

See [Flickr search parameters](https://www.flickr.com/services/api/flickr.photos.search.html).

## Adding a source

Write `sources/<name>.py` with a class that has `name` and
`search(query, domain_key, limit) -> list[Candidate]`, register it in
`sources/__init__.py:build_source`, and add parser tests with a saved JSON response in
`tests/prompt_generation/fixtures/`.
