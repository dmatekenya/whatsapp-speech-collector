"""Study boundaries exercised through the real webhook, worker and Postgres."""

from datetime import UTC, date, datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from scripts.enroll import main as enroll
from scripts.import_participants import main as import_participants
from src.registry.enrollments import EXPERIMENT, MAIN, active_enrollment
from src.storage import db
from src.storage.duplicates import EXPERIMENT_DUPLICATE
from src.storage.models import Assignment, Enrollment, InboundMessage, Participant, Recording, TaskSession, main_recordings
from src.tasks.bank import enrollment_limits, lifetime_minutes, prompt_coverage
from src.workers.jobs import process_inbound
from tests import meta_payloads as mp
from tests.test_meta_pipeline import WA_ID, channels, config, from_meta  # noqa: F401

PARTICIPANT = "P0001"


def _clip(sid: str) -> Recording:
    with db.get_sync_sessionmaker()() as session:
        return session.scalars(
            select(Recording).join(InboundMessage).where(InboundMessage.message_sid == sid)
        ).one()


def _active() -> Enrollment:
    with db.get_sync_sessionmaker()() as session:
        return active_enrollment(session, PARTICIPANT)


def _enter_main_session(client) -> None:
    from_meta(client, mp.text(WA_ID, "Hi"))
    from_meta(client, mp.text(WA_ID, "1"))


def _enqueue_voice(client) -> str:
    message = mp.voice_note(WA_ID)
    body, headers = mp.signed(mp.event([message]))
    assert client.post("/webhooks/meta", content=body, headers=headers).status_code == 200
    return message["id"]


def test_main_ready_and_voice_keep_the_same_prompt_flow(client, channels):
    enrollment = _active()
    _enter_main_session(client)
    assert "Sentence 1 of" in channels.meta.sent[-1][2]
    sid = from_meta(client, mp.voice_note(WA_ID))
    clip = _clip(sid)
    assert clip.enrollment_id == enrollment.id
    assert clip.qc_status == "accepted"
    with db.get_sync_sessionmaker()() as session:
        assignment = session.get(Assignment, clip.assignment_id)
        assert session.get(TaskSession, assignment.session_id).enrollment_id == enrollment.id
    assert "Sentence 2 of" in channels.meta.sent[-1][2]


def test_switch_uses_receipt_time_and_pauses_main_session(client, channels):
    main = _active()
    _enter_main_session(client)
    main_pending = _enqueue_voice(client)
    enroll(["enroll", PARTICIPANT, EXPERIMENT, "stage1"])
    experiment = _active()

    with db.get_sync_sessionmaker()() as session:
        assert session.scalar(select(TaskSession.status)) == "paused"
        assert session.scalar(select(Assignment.status)) == "expired"
        prompt_id = session.scalar(select(Assignment.prompt_id))
        assert prompt_coverage(session, prompt_id) == 0

    experiment_pending = _enqueue_voice(client)
    enroll(["resume", PARTICIPANT, MAIN, "pilot"])
    _enter_main_session(client)
    process_inbound(experiment_pending)
    assert "paused" in channels.meta.sent[-1][2]
    experiment_clip = _clip(experiment_pending)
    assert experiment_clip.enrollment_id == experiment.id
    assert experiment_clip.assignment_id is None
    assert experiment_clip.qc_status == "pending"

    # The older queued main clip retains its original enrollment.
    process_inbound(main_pending)
    assert _clip(main_pending).enrollment_id == main.id
    assert _clip(main_pending).assignment_id is None
    with db.get_sync_sessionmaker()() as session:
        ids = session.scalars(select(main_recordings.c.id)).all()
    assert experiment_clip.id not in ids


def test_experiment_cannot_mark_an_existing_main_clip_duplicate(client, channels):
    first_sid = from_meta(client, mp.voice_note(WA_ID))
    enroll(["enroll", PARTICIPANT, EXPERIMENT, "stage1"])
    study_sid = from_meta(client, mp.voice_note(WA_ID))
    first = _clip(first_sid)
    assert first.duplicate_of_recording_id is None
    assert first.qc_status == "accepted" and first.qc_reason is None
    assert _clip(study_sid).content_sha256 == first.content_sha256


def test_new_main_clip_matching_experiment_has_a_distinct_flag(client, channels):
    enroll(["enroll", PARTICIPANT, EXPERIMENT, "stage1"])
    study_sid = from_meta(client, mp.voice_note(WA_ID))
    enroll(["resume", PARTICIPANT, MAIN, "pilot"])
    _enter_main_session(client)
    main_sid = from_meta(client, mp.voice_note(WA_ID, **mp.forwarded_context()))
    clip = _clip(main_sid)
    assert clip.content_sha256 == _clip(study_sid).content_sha256
    assert (clip.qc_status, clip.qc_reason) == ("rejected", EXPERIMENT_DUPLICATE)
    assert clip.assignment_id is None and clip.duplicate_of_recording_id is None
    assert "Sentence 1 of" in channels.meta.sent[-1][2]


def test_experiment_duplicate_matches_only_another_experiment_clip(client, channels):
    main_sid = from_meta(client, mp.voice_note(WA_ID))
    enroll(["enroll", PARTICIPANT, EXPERIMENT, "stage1"])
    experiment_sid = from_meta(client, mp.voice_note(WA_ID))
    repeat_sid = from_meta(client, mp.voice_note(WA_ID, **mp.forwarded_context()))
    main, original, repeat = map(_clip, (main_sid, experiment_sid, repeat_sid))
    assert main.content_sha256 == original.content_sha256 == repeat.content_sha256
    assert original.duplicate_of_recording_id is None
    assert repeat.duplicate_of_recording_id == original.id
    assert repeat.audio_origin == "forwarded"
    assert (repeat.qc_status, repeat.qc_reason) == ("pending", None)


def test_late_main_voice_note_cannot_be_flagged_by_a_newer_experiment_clip(client, channels):
    main_sid = _enqueue_voice(client)
    enroll(["enroll", PARTICIPANT, EXPERIMENT, "stage1"])
    from_meta(client, mp.voice_note(WA_ID))
    process_inbound(main_sid)
    assert (_clip(main_sid).qc_status, _clip(main_sid).qc_reason) == ("accepted", None)


def test_import_reuses_phone_and_refuses_the_same_round(clean_db, tmp_path, capsys):
    path = tmp_path / "pilot.csv"
    path.write_text(f"name,phone_number\nTest Person,+{WA_ID}\n", encoding="utf-8")
    for round_name in ("pilot", "round1"):
        with pytest.raises(SystemExit) as exit_:
            import_participants([str(path), "--round", round_name, "--no-send-welcome"])
        assert exit_.value.code == 0
    assert "re-enrolled: 1" in capsys.readouterr().out
    with db.get_sync_sessionmaker()() as session:
        participant = session.scalars(select(Participant)).one()
        assert participant.lifetime_cap_minutes == 80
        assert session.execute(select(Enrollment.round, Enrollment.status).order_by(Enrollment.id)).all() == [
            ("pilot", "paused"), ("round1", "active"),
        ]
    with pytest.raises(SystemExit) as exit_:
        import_participants([str(path), "--round", "round1", "--no-send-welcome"])
    assert exit_.value.code == 1


def test_database_rejects_a_second_active_enrollment(registered_sender):
    with db.get_sync_sessionmaker()() as session:
        session.add(Enrollment(participant_id=PARTICIPANT, study=EXPERIMENT, round="stage1", status="active"))
        with pytest.raises(IntegrityError, match="uq_enrollments_active"):
            session.flush()


def _seed_clip(session, enrollment: Enrollment, sid: str, minutes: float, qc_status: str) -> Recording:
    inbound = InboundMessage(message_sid=sid, sender=f"+{WA_ID}", message_type="audio", raw_payload={})
    session.add(inbound)
    session.flush()
    recording = Recording(
        participant_id=PARTICIPANT, enrollment_id=enrollment.id, inbound_message_id=inbound.id,
        status="stored", duration_s=minutes * 60, qc_status=qc_status,
    )
    session.add(recording)
    session.flush()
    return recording


def test_lifetime_cap_counts_unassigned_pending_across_main_rounds(client, channels, monkeypatch):
    pilot = _active()
    enroll(["enroll", PARTICIPANT, EXPERIMENT, "stage1"])
    experiment = _active()
    enroll(["enroll", PARTICIPANT, MAIN, "round1"])
    with db.get_sync_sessionmaker().begin() as session:
        participant = session.scalar(select(Participant))
        participant.lifetime_cap_minutes = 80
        _seed_clip(session, pilot, "prior", 79, "accepted")
        _seed_clip(session, experiment, "experiment", 90, "pending")
        round1 = active_enrollment(session, PARTICIPANT)
        round1.target_minutes = 10
        with monkeypatch.context() as settings:
            settings.setattr("src.tasks.bank.lifetime_studies", lambda: (MAIN, EXPERIMENT))
            assert lifetime_minutes(session, PARTICIPANT) == 169
        assert enrollment_limits(session, participant, round1, config()).effective_target == 1
    _enter_main_session(client)
    assert "Sentence 1 of" in channels.meta.sent[-1][2]

    with db.get_sync_sessionmaker().begin() as session:
        _seed_clip(session, active_enrollment(session, PARTICIPANT), "new_pending", 1, "pending")
        assert lifetime_minutes(session, PARTICIPANT) == 80
    from_meta(client, mp.text(WA_ID, "Hi"))
    assert _active() is None
    assert "completed your recordings" in channels.meta.sent[-1][2]
    sid = from_meta(client, mp.voice_note(WA_ID))
    with db.get_sync_sessionmaker()() as session:
        assert not session.scalar(select(Recording.id).join(InboundMessage).where(InboundMessage.message_sid == sid))


def test_round_ceiling_stops_the_next_assignment(client, channels):
    _enter_main_session(client)
    with db.get_sync_sessionmaker().begin() as session:
        enrollment = active_enrollment(session, PARTICIPANT)
        enrollment.target_minutes = 1
        task_session = session.scalar(select(TaskSession).where(TaskSession.status == "active"))
        earlier = _seed_clip(session, enrollment, "previous-pending", 59 / 60, "pending")
        assignment = Assignment(
            participant_id=PARTICIPANT, session_id=task_session.id, prompt_type="sentence",
            position=2, status="recorded", recording_id=earlier.id,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        session.add(assignment)
        session.flush()
        earlier.assignment_id = assignment.id
        assert lifetime_minutes(session, PARTICIPANT) == pytest.approx(59 / 60)

    from_meta(client, mp.voice_note(WA_ID))
    assert "completed your recordings" in channels.meta.sent[-1][2]
    assert _active() is None
    with db.get_sync_sessionmaker()() as session:
        assert session.scalars(select(Assignment.status).order_by(Assignment.position)).all() == [
            "recorded", "recorded",
        ]


def test_upgrade_downgrade_upgrade_backfills_existing_rows(clean_db):
    alembic = Config("alembic.ini")
    down = "e2a6c4f8b913"
    command.downgrade(alembic, down)
    try:
        with db.get_sync_engine().begin() as connection:
            connection.execute(text("""
                INSERT INTO participants (participant_id, name, phone_number, status, batch, enrolled_on, source_file)
                VALUES ('P0001', 'Fixture', '+265991000000', 'inactive', 'pilot', :enrolled_on, 'test')
            """), {"enrolled_on": date(2026, 1, 1)})
            connection.execute(text("""
                INSERT INTO sessions (participant_id, task_type, size) VALUES ('P0001', 'sentence', 3)
            """))
            connection.execute(text("""
                INSERT INTO inbound_messages (message_sid, sender, message_type, raw_payload)
                VALUES ('linked', '+265991000000', 'audio', '{}'), ('unknown', '+265991000001', 'audio', '{}')
            """))
            connection.execute(text("""
                INSERT INTO recordings (inbound_message_id, participant_id)
                SELECT id, CASE WHEN message_sid = 'linked' THEN 'P0001' END FROM inbound_messages
            """))
        command.upgrade(alembic, "head")
        command.downgrade(alembic, down)
        command.upgrade(alembic, "head")
        with db.get_sync_engine().connect() as connection:
            assert connection.execute(text("""
                SELECT e.round, e.status, p.lifetime_cap_minutes, s.enrollment_id = e.id
                FROM enrollments e JOIN participants p USING (participant_id)
                JOIN sessions s USING (participant_id)
            """)).one() == ("pilot", "paused", 80, True)
            assert connection.execute(text("""
                SELECT m.message_sid, r.enrollment_id IS NOT NULL
                FROM main_recordings r JOIN inbound_messages m ON m.id = r.inbound_message_id
                ORDER BY m.message_sid
            """)).all() == [("linked", True), ("unknown", False)]
    finally:
        command.upgrade(alembic, "head")
