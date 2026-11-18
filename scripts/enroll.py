"""Manage study enrollments without creating new participant records.

    uv run python -m scripts.enroll enroll P0001 qc_calibration stage1
    uv run python -m scripts.enroll resume P0001 main pilot
    uv run python -m scripts.enroll list P0001
"""

import argparse

from sqlalchemy import select

from src.conversation.locks import lock_conversation
from src.registry.enrollments import EXPERIMENT, MAIN, activate
from src.storage.db import get_sync_sessionmaker
from src.storage.models import Enrollment, Participant


# ===
# CLI
# ===
def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("enroll", "resume", "list"):
        command = subcommands.add_parser(name)
        command.add_argument("participant_id")
        if name != "list":
            command.add_argument("study", choices=(MAIN, EXPERIMENT))
            command.add_argument("round")
    args = parser.parse_args(argv)

    with get_sync_sessionmaker().begin() as db:
        participant = db.scalar(select(Participant).where(Participant.participant_id == args.participant_id))
        if participant is None:
            parser.error("Participant does not exist")
        query = select(Enrollment).where(Enrollment.participant_id == args.participant_id)
        if args.command == "list":
            for enrollment in db.scalars(query.order_by(Enrollment.id)):
                print(f"{enrollment.study}\t{enrollment.round}\t{enrollment.status}")
            return
        lock_conversation(db, participant.phone_number)
        enrollment = db.scalar(query.where(Enrollment.study == args.study, Enrollment.round == args.round))
        if enrollment is None:
            if args.command == "resume":
                parser.error("Enrollment does not exist")
            enrollment = Enrollment(participant_id=participant.participant_id, study=args.study, round=args.round)
        if args.command == "resume" and enrollment.status not in {"paused", "active"}:
            parser.error("Only a paused or active enrollment can be resumed")
        activate(db, participant, enrollment)
    print(f"{args.participant_id}\t{args.study}\t{args.round}\tactive")


if __name__ == "__main__":
    main()
