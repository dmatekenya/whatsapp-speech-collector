"""Import a participant batch CSV into the registry.

    uv run python scripts/import_participants.py data/participants/batch.csv --dry-run
    uv run python scripts/import_participants.py data/participants/batch.csv --write-ids

Re-importing the same file is safe: rows are matched by participant_id, or by
phone number when participant_id is blank, and updated or left alone.
"""

import argparse
import sys
from pathlib import Path

from src.importers.framework import run_import
from src.importers.participants import DEFAULT_CONFIG, ParticipantImporter
from src.storage.db import get_sync_sessionmaker


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="validate and report, write nothing")
    parser.add_argument(
        "--write-ids",
        action="store_true",
        help="also write <name>_imported.csv with participant IDs (the original is never changed)",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()

    importer = ParticipantImporter.from_file(args.csv, args.config)
    report = run_import(
        importer, args.csv, get_sync_sessionmaker(), dry_run=args.dry_run, write_ids=args.write_ids
    )
    print(report.render())
    sys.exit(0 if report.ok else 1)


if __name__ == "__main__":
    main()
