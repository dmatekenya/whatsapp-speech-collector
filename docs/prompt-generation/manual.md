# Prompt-generation manual

**Status: partial; the Google Docs manual has not yet been supplied.** The following reflects the current importer and methodology, rather than a complete approved annotator manual.

## Image handover

```text
images/
  round-1/
    community_life/
      market_scene_001.jpg
    nature_and_landscape/
      lakeside_scene_001.png
```

Store batches under `data/prompts/images/<batch>/<theme>/`. The batch is the selected folder's name; the first subfolder determines the theme (`domain`). Use consistent lowercase names with underscores. Put every image in a theme folder; top-level files import without a theme. Deeper folders do not add theme levels.

Accepted formats: JPG, JPEG, PNG and WebP; source limit: 50 MB. Descriptive filenames are recommended, but no naming pattern is enforced. The importer assigns `I0001`-style IDs. Files and folders beginning with `_` or `.` are skipped.

One source label applies to each import (`ai-generated`, `own-photos`, or a licence/source label), so separate batches by provenance when needed. Provide provenance and permission details separately; these are not read automatically from a manifest. Settle unresolved provenance before dataset release.

Avoid duplicate and near-duplicate pictures; byte hashing catches identical files, not all visual duplicates. Prepared images are generated automatically as metadata-free RGB JPEGs, maximum long side 1,600 px and maximum size 5 MB, under `_prepared/<theme>/I0001.jpg`. Submit originals, not prepared copies. `--write-ids` writes a companion CSV mapping files to prompt IDs.

## Text handover

Sentence CSVs live under `data/prompts/text/`; conversation-starter CSVs under `data/prompts/conversation-starters/`. Both require `text` and `source`; `domain` is optional. Use a new file per batch. Exact column validation is defined in `config/prompts.yaml`.

## Source documents pending

Add the approved Google Docs export, including author, version and source URL. Language guidance, topic coverage and examples will be incorporated from that source rather than invented here.
