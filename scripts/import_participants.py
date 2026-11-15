"""Import a participant batch CSV into the registry.

    uv run python scripts/import_participants.py data/participants/batch.csv --dry-run
    uv run python scripts/import_participants.py data/participants/batch.csv --write-ids
    uv run python scripts/import_participants.py data/participants/batch.csv --batch pilot

Each participant's batch is the CSV's batch column if it has a value, otherwise
--batch, otherwise the file name without extension ("batch" above). Like the
other defaults, it is set for new participants only: a re-import never changes
an existing participant's batch unless the batch column says so.

Re-importing the same file is safe: rows are matched by participant_id, or by
phone number when participant_id is blank, and updated or left alone.

--send-welcome sends the registration welcome (a WhatsApp template, see
scripts/send_welcome.py) to the participants this import created, after a
successful import (never on a dry run; test participants are skipped). It is
the default when send_on_import is true in config/welcome.yaml;
--no-send-welcome turns it off. Exits 1 if the import or any send failed.
"""

import argparse
import sys
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from src.importers.framework import run_import
from src.importers.participants import DEFAULT_CONFIG, ParticipantImporter
from src.logging_config import setup_logging
from src.storage.db import get_sync_sessionmaker


def _batch(text: str) -> str:
    value = " ".join(text.split())
    if not value:
        raise argparse.ArgumentTypeError("must not be empty")
    return value


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="validate and report, write nothing")
    parser.add_argument(
        "--write-ids",
        action="store_true",
        help="also write <name>_imported.csv with participant IDs (the original is never changed)",
    )
    parser.add_argument(
        "--batch",
        type=_batch,
        metavar="NAME",
        help="batch for rows without a batch value (default: the file name without extension)",
    )
    parser.add_argument(
        "--send-welcome",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="send the registration welcome to new participants (default: send_on_import in config/welcome.yaml)",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)

    importer = ParticipantImporter.from_file(args.csv, args.config, batch=args.batch)
    factory = get_sync_sessionmaker()
    report = run_import(importer, args.csv, factory, dry_run=args.dry_run, write_ids=args.write_ids)
    print(report.render())
    if not report.ok:
        sys.exit(1)
    sys.exit(0 if _welcome(args, importer, factory) else 1)


def _welcome(args: argparse.Namespace, importer: ParticipantImporter, factory: sessionmaker[Session]) -> bool:
    """Send the welcome to the new participants if asked; False if any send failed."""
    from scripts.send_welcome import print_results
    from src.registry.welcome import (
        WelcomeConfigError,
        get_welcome_config,
        select_participants,
        send_welcome,
    )

    try:
        welcome = get_welcome_config()
    except WelcomeConfigError as exc:
        if args.send_welcome:
            print(f"Welcome not sent: config/welcome.yaml: {exc}", file=sys.stderr)
            return False
        return True
    if not (welcome.send_on_import if args.send_welcome is None else args.send_welcome):
        return True
    if args.dry_run:
        print("Welcome: not sent (dry run).")
        return True
    if not importer.new_ids:
        print("Welcome: no new participants, nothing to send.")
        return True
    setup_logging()
    with factory() as db:
        candidates = select_participants(db, importer.new_ids, welcome=welcome)
    print(f"Welcome ({welcome.template}, {welcome.language}) to {len(importer.new_ids)} new participant(s):")
    results = send_welcome(factory, candidates, welcome=welcome)
    print_results(results)
    return not any(r.outcome == "failed" for r in results)


if __name__ == "__main__":
    main()
