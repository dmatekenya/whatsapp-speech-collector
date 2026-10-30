"""Import a batch of prompts into the prompt bank (a CSV, or a folder for images).

    uv run python -m scripts.import_prompts sentences data/prompts/text/batch.csv --dry-run
    uv run python -m scripts.import_prompts conversation_starters data/prompts/conversation-starters/batch.csv
    uv run python -m scripts.import_prompts images data/prompts/images/batch --source ai-generated

Prompts already in the bank (same normalized text, or for images the same
file bytes) are reported and skipped, so re-importing a batch is safe.

Valid rows are imported and invalid ones skipped (in one transaction); the
rejected rows are written to <name>_rejected.csv with a rejection_reason
column. Use --strict to import nothing unless every row is valid.
"""

import argparse
import sys
from pathlib import Path

from src.importers.framework import BatchImporter, load_config, run_import
from src.importers.images import ImagePromptImporter, SkippedFolderError
from src.importers.prompts import DEFAULT_CONFIG, PromptImporter
from src.storage.db import get_sync_sessionmaker

IMAGE_FOLDER = "image_folder"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("prompt_type", help="a key under prompt_types in config/prompts.yaml")
    parser.add_argument("path", type=Path, help="CSV file, or the batch folder for image prompts")
    parser.add_argument("--dry-run", action="store_true", help="validate and report, write nothing")
    parser.add_argument(
        "--write-ids",
        action="store_true",
        help="also write <name>_imported.csv with prompt IDs (the original is never changed)",
    )
    parser.add_argument(
        "--strict", action="store_true", help="all-or-nothing: import nothing if any row is invalid"
    )
    parser.add_argument("--source", help="image prompts only: source for the whole batch")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()

    config = load_config(args.config)
    types = config.options["prompt_types"]
    if args.prompt_type not in types:
        parser.error(f"unknown prompt type {args.prompt_type!r}; configured: {', '.join(types)}")

    importer: BatchImporter
    if types[args.prompt_type].get("kind") == IMAGE_FOLDER:
        if not args.path.is_dir():
            parser.error(f"{args.prompt_type} imports a folder; {args.path} isn't one")
        importer = ImagePromptImporter(config, args.prompt_type, source=args.source)
    else:
        if args.source is not None:
            parser.error("--source only applies to image prompts; CSVs have a source column")
        importer = PromptImporter(config, args.prompt_type, source_file=args.path.name)

    try:
        report = run_import(
            importer,
            args.path,
            get_sync_sessionmaker(),
            dry_run=args.dry_run,
            write_ids=args.write_ids,
            strict=args.strict,
        )
    except SkippedFolderError as exc:
        parser.error(str(exc))
    print(report.render())
    sys.exit(0 if report.ok else 1)


if __name__ == "__main__":
    main()
