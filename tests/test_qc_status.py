"""Tier 1 outcome (recordings.qc_status): migration e2a6c4f8b913 moves clips the engine has
finished from 'pending' to 'accepted' (duplicates to rejected), leaving progress unchanged."""

from alembic import command
from alembic.config import Config
from sqlalchemy import update

from src.storage import db
from src.storage.models import Assignment, InboundMessage, Recording
from src.tasks.bank import minutes_recorded
from tests.test_qc_tier3 import person, recording, state  # noqa: F401 - person is a fixture

BEFORE = "d9f4b2c8e631"


def _set(recording_id: int, **values: object) -> None:
    with db.get_sync_sessionmaker()() as session:
        session.execute(update(Recording).where(Recording.id == recording_id).values(**values))
        session.commit()


def _count_it(recording_id: int) -> None:
    """Make the clip its assignment's current recording, as the task bank does."""
    with db.get_sync_sessionmaker()() as session:
        clip = session.get(Recording, recording_id)
        session.execute(update(Assignment).where(Assignment.id == clip.assignment_id).values(recording_id=clip.id))
        session.commit()


def _minutes() -> dict[str, float]:
    with db.get_sync_sessionmaker()() as session:
        return minutes_recorded(session, "P0001")


def test_migration_accepts_finished_pending_clips(person: str) -> None:  # noqa: F811
    alembic = Config("alembic.ini")
    command.downgrade(alembic, BEFORE)
    try:
        sentence = recording("sentence", "pending", qc_tier2=4)
        weak = recording("sentence", "pending", qc_tier2=2)
        image = recording("image", "pending")
        unassigned = recording(None, "pending")
        duplicate = recording(None, "pending")
        _set(duplicate, duplicate_of_recording_id=unassigned)
        unfinished = recording("sentence", "pending", qc_tier2=4)  # its turn failed: still unchecked
        with db.get_sync_sessionmaker()() as session:
            message_id = session.get(Recording, unfinished).inbound_message_id
            session.execute(update(InboundMessage).where(InboundMessage.id == message_id).values(status="failed"))
            session.commit()
        flagged = recording("free_speech", "needs_review")
        too_short = recording("sentence", "rejected")
        for rid in (sentence, weak, image, flagged):
            _count_it(rid)
        progress = _minutes()
        assert {rid: state(rid).qc_final for rid in (sentence, image)} == {sentence: "bronze", image: "bronze"}
        updated = state(sentence).updated_at

        command.upgrade(alembic, "head")
        after = {rid: (state(rid).qc_status, state(rid).qc_reason, state(rid).qc_final) for rid in
                 (sentence, weak, image, unassigned, duplicate, unfinished, flagged, too_short)}  # fmt: skip
        assert after == {
            sentence: ("accepted", None, "silver"),
            weak: ("accepted", None, "bronze"),
            image: ("accepted", None, "silver"),
            unassigned: ("accepted", None, "bronze"),
            duplicate: ("rejected", "duplicate", "rejected"),
            unfinished: ("pending", None, "bronze"),
            flagged: ("needs_review", None, "bronze"),
            too_short: ("rejected", None, "rejected"),
        }
        assert _minutes() == progress
        assert state(sentence).updated_at > updated  # qc_final changed: the dashboard's sync sees it

        command.downgrade(alembic, BEFORE)
        assert [state(rid).qc_status for rid in (sentence, image, unassigned, duplicate, unfinished)] == ["pending"] * 5
        assert state(duplicate).qc_reason is None and _minutes() == progress
    finally:
        command.upgrade(alembic, "head")
