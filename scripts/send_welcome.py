"""Send the registration welcome (a WhatsApp template) to registered participants.

On the droplet (inside the api container):

    docker compose exec api python -m scripts.send_welcome --dry-run --batch pilot
    docker compose exec api python -m scripts.send_welcome --batch pilot
    docker compose exec api python -m scripts.send_welcome --include-test --participants P0006 --force

Sent to active, non-test participants who have never been sent this template and
have never written to the bot. --participants and --batch narrow the selection
(with neither, every participant is considered). --include-test includes test
participants; --force sends again to participants already sent it, or already
active in the bot. Inactive participants are never sent it.

Template, language and the import default are in config/welcome.yaml; the
template must be Active in WhatsApp Manager. It goes out on the Meta number.
Each send is recorded in outbound_messages (purpose "template:<name>"), and the
webhook adds delivered/read/failed. Exits 1 if any send failed; failures are
not retried automatically.

Prints participant IDs (and, with --dry-run, first names and targets); never
phone numbers.
"""

import argparse
import sys

from src.logging_config import setup_logging
from src.registry.welcome import (
    Candidate,
    Result,
    WelcomeConfigError,
    get_welcome_config,
    select_participants,
    send_welcome,
)
from src.storage.db import get_sync_sessionmaker


def _ids(text: str) -> list[str]:
    ids = [part.strip().upper() for part in text.split(",") if part.strip()]
    if not ids:
        raise argparse.ArgumentTypeError("expected participant IDs like P0008,P0009")
    return ids


def _batch(text: str) -> str:
    value = " ".join(text.split())
    if not value:
        raise argparse.ArgumentTypeError("must not be empty")
    return value


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--participants", type=_ids, default=[], metavar="P0008,P0009")
    parser.add_argument("--batch", type=_batch, metavar="NAME", help="only participants of this batch")
    parser.add_argument("--include-test", action="store_true", help="include test participants (is_test)")
    parser.add_argument("--force", action="store_true", help="send again, or to participants already active in the bot")
    parser.add_argument("--dry-run", action="store_true", help="list who would be sent it; send nothing")
    return parser.parse_args(argv)


def print_selection(candidates: list[Candidate]) -> None:
    to_send = [c for c in candidates if not c.skip]
    print(f"Would send to {len(to_send)} participant(s):")
    for c in to_send:
        print(f"  {c.participant_id}  {c.first_name}  target {c.target} min")
    for c in candidates:
        if c.skip:
            print(f"  {c.participant_id}  skipped: {c.skip}")


def print_results(results: list[Result]) -> None:
    for r in results:
        print(f"  {r.participant_id}  {r.outcome}" + (f": {r.detail}" if r.detail else ""))
    counts = {outcome: sum(r.outcome == outcome for r in results) for outcome in ("sent", "failed", "skipped")}
    print(f"Sent {counts['sent']}, failed {counts['failed']}, skipped {counts['skipped']}.")
    if counts["failed"]:
        print(
            "Not retried. If Meta refused the template (e.g. error 132001 name/language, 132000 values,"
            " 132015/132016 paused or disabled), check it is Active in WhatsApp Manager and matches"
            " config/welcome.yaml, then run again.",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging()
    try:
        welcome = get_welcome_config()
    except WelcomeConfigError as exc:
        print(f"config/welcome.yaml: {exc}", file=sys.stderr)
        return 2
    factory = get_sync_sessionmaker()
    with factory() as db:
        candidates = select_participants(
            db, args.participants, args.batch, include_test=args.include_test, force=args.force, welcome=welcome
        )
    print(f"Template {welcome.template} ({welcome.language}).")
    if args.dry_run:
        print("DRY RUN: nothing sent.")
        print_selection(candidates)
        return 0
    if not candidates:
        print("No participants selected.")
        return 0
    results = send_welcome(factory, candidates, welcome=welcome)
    print_results(results)
    return 1 if any(r.outcome == "failed" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
