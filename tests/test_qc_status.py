"""Tier 1 outcome (recordings.qc_status): migration e2a6c4f8b913 moves clips the engine has
finished from 'pending' to 'accepted' (duplicates to rejected), leaving progress unchanged."""

from alembic import command
from alembic.config import Config
from sqlalchemy import text, update

from src.storage import db
from src.storage.models import Assignment, InboundMessage, Recording
from src.tasks.bank import minutes_recorded
from src.tasks.config import get_collection_config
from tests.test_qc_tier3 import person, recording  # noqa: F401 - person is a fixture

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


def _state(recording_id: int):
    with db.get_sync_engine().connect() as connection:
        return connection.execute(text("""
            SELECT qc_status, qc_reason, qc_final, updated_at
            FROM recordings WHERE id = :recording_id
        """), {"recording_id": recording_id}).one()


def _historical_minutes() -> dict[str, float]:
    totals = dict.fromkeys(get_collection_config().task_types, 0.0)
    with db.get_sync_engine().connect() as connection:
        for task_type, seconds in connection.execute(text("""
            SELECT a.prompt_type, sum(r.duration_s)
            FROM assignments a JOIN recordings r ON r.id = a.recording_id
            WHERE a.participant_id = 'P0001' AND a.status IN ('recorded', 'accepted')
              AND r.status = 'stored' AND r.qc_status != 'rejected'
              AND r.duplicate_of_recording_id IS NULL
            GROUP BY a.prompt_type
        """)):
            totals[task_type] = seconds / 60
    return totals


def test_migration_accepts_finished_pending_clips(person: str) -> None:  # noqa: F811
    alembic = Config("alembic.ini")
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
        command.downgrade(alembic, BEFORE)
        progress = _historical_minutes()
        assert {rid: _state(rid).qc_final for rid in (sentence, image)} == {sentence: "bronze", image: "bronze"}
        updated = _state(sentence).updated_at

        command.upgrade(alembic, "head")
        after = {rid: (_state(rid).qc_status, _state(rid).qc_reason, _state(rid).qc_final) for rid in
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
        assert _state(sentence).updated_at > updated  # qc_final changed: the dashboard's sync sees it

        command.downgrade(alembic, BEFORE)
        assert [_state(rid).qc_status for rid in (sentence, image, unassigned, duplicate, unfinished)] == ["pending"] * 5
        assert _state(duplicate).qc_reason is None and _historical_minutes() == progress
    finally:
        command.upgrade(alembic, "head")
