"""Signed webhook tests for the isolated QC calibration flow."""

import csv
import io
import wave
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text

from scripts.enroll import main as enroll
from scripts.export_calibration_clips import main as export
from src.registry.enrollments import EXPERIMENT, MAIN, active_enrollment
from src.storage import db
from src.storage.models import Assignment, CalibrationClip, CalibrationState, Enrollment, Prompt, Recording
from src.studies.qc_calibration.config import get_config
from src.tasks.bank import lifetime_minutes, minutes_recorded, prompt_coverage
from src.workers.jobs import process_inbound
from tests import meta_payloads as mp
from tests.test_meta_pipeline import WA_ID, from_meta, outbound_rows
from tests.test_meta_pipeline import channels as channels


@pytest.fixture
def study(client, channels, monkeypatch):
    monkeypatch.setattr(get_config(), "enabled", True)
    with db.get_sync_sessionmaker().begin() as session:
        session.add_all([
            Prompt(prompt_id="S12577", prompt_type="sentence", text="Fixed sentence.", source_file="fixture", active=False),
            Prompt(prompt_id="C0482", prompt_type="conversation_starter", text="Fixed conversation?", source_file="fixture", active=False),
        ])
    enroll(["enroll", "P0001", EXPERIMENT, "stage1"])
    # Exercise Meta's actual interactive serializer and staging send stub.
    monkeypatch.setattr(channels.meta, "_sends_disabled", True)
    return channels


def interactive(client, reply_id, kind):
    message = mp.text(WA_ID, "Unused")
    message.pop("text")
    message.update(type="interactive", interactive={
        "type": kind, kind: {"id": reply_id, "title": "Display title is not the action"},
    })
    return from_meta(client, message)


def choose(client, condition):
    menu = outbound_rows()[-1]
    rows = menu.interactive["action"]["sections"][0]["rows"]
    assert len(rows) == 10 and menu.provider_message_id.startswith("stub-")
    row = next(row for row in rows if row["id"].endswith(f":{condition}"))
    interactive(client, row["id"], "list_reply")


def tap(client, action):
    buttons = outbound_rows()[-1].interactive["action"]["buttons"]
    reply = next(button["reply"] for button in buttons if button["reply"]["id"].endswith(f":{action}"))
    return interactive(client, reply["id"], "button_reply")


def clip(client):
    return from_meta(client, mp.voice_note(WA_ID))


def progress(cutoff):
    with db.get_sync_sessionmaker()() as session:
        ids = session.scalars(select(Prompt.prompt_id)).all()
        return (
            session.scalar(select(func.count()).select_from(Assignment)),
            (lifetime_minutes(session, "P0001"), minutes_recorded(session, "P0001")),
            {prompt_id: prompt_coverage(session, prompt_id, cutoff) for prompt_id in ids},
        )


def test_flag_off_uses_paused_reply(client, channels):
    assert not get_config().enabled
    enroll(["enroll", "P0001", EXPERIMENT, "stage1"])
    from_meta(client, mp.text(WA_ID, "Hi"))
    row = outbound_rows()[-1]
    assert row.interactive is None and "paused" in row.body
    with db.get_sync_sessionmaker()() as session:
        assert session.scalar(select(func.count()).select_from(CalibrationState)) == 0


def test_off_suggestion_retake_and_main_coverage(client, study):
    cutoff = datetime.now(UTC)
    before = progress(cutoff)
    from_meta(client, mp.text(WA_ID, "Hi"))
    menu = outbound_rows()[-1].interactive["action"]["sections"][0]["rows"]
    suggested = next(row["id"].split(":")[-1] for row in menu if "Suggested" in row["description"])
    selected = "C1" if suggested != "C1" else "C2"
    choose(client, selected)
    tap(client, "ready")
    expected = ["sentence", "conversation_starter", "free_speech"]
    first = clip(client)
    process_inbound(first)
    tap(client, "retake")
    clip(client)
    tap(client, "next")
    for _ in expected[1:]:
        clip(client)
        tap(client, "next")
    with db.get_sync_sessionmaker()() as session:
        clips = session.scalars(select(CalibrationClip).where(
            CalibrationClip.condition_code == selected,
        ).order_by(CalibrationClip.id)).all()
        assert [row.task_type for row in clips] == [expected[0], *expected]
        assert [row.attempt for row in clips] == [1, 2, *([1] * (len(expected) - 1))]
        assert all(row.suggested_condition == suggested for row in clips)
        assert {row.order_position for row in clips} == {1}
        assert [row.prompt_id for row in clips] == ["S12577", "S12577", "C0482", None]
        assert all(session.get(Recording, row.recording_id).assignment_id is None for row in clips)
    assert {"S12577", "C0482"} <= before[2].keys()
    assert progress(cutoff) == before


def test_c3_skips_sentence_and_all_conditions_resume_main(client, study):
    from_meta(client, mp.text(WA_ID, "Hi"))
    for condition in ("C3", *[code for code in get_config().conditions if code != "C3"]):
        choose(client, condition)
        tap(client, "ready")
        if condition == "C3":
            assert "Fixed conversation?" in outbound_rows()[-1].body
        for _ in get_config().conditions[condition].tasks:
            clip(client)
            tap(client, "next")
    assert "Thank you" in outbound_rows()[-1].body
    with db.get_sync_sessionmaker()() as session:
        assert active_enrollment(session, "P0001").study == MAIN
        assert session.scalar(select(Enrollment.status).where(Enrollment.study == EXPERIMENT)) == "completed"
        assert session.scalar(select(func.count()).select_from(CalibrationClip)) == 29


def test_silence_is_stored_with_observational_qc(client, study, tmp_path, monkeypatch):
    audio = io.BytesIO()
    with wave.open(audio, "wb") as handle:
        handle.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        handle.writeframes(b"\0\0" * 16000 * 11)
    study.meta.media = audio.getvalue()
    from_meta(client, mp.text(WA_ID, "Hi"))
    choose(client, "C9")
    tap(client, "ready")
    assert "10–15 seconds" in outbound_rows()[-1].body
    clip(client)
    with db.get_sync_sessionmaker()() as session:
        result = session.scalars(select(CalibrationClip)).one()
        recording = session.get(Recording, result.recording_id)
        assert result.task_type == "silence" and result.prompt_id is None
        assert result.fast_qc["speech_problem"] == "no_speech"
        assert recording.status == "stored" and recording.qc_status != "rejected"


    restricted = tmp_path / "restricted"
    monkeypatch.setattr("sys.argv", ["export_calibration_clips", str(restricted)])
    export()
    with (restricted / "clips.csv").open() as handle:
        metadata = list(csv.DictReader(handle))
    assert metadata[0]["participant_id"] == "P0001"
    assert metadata[0]["condition"] == "C9" and metadata[0]["speech_problem"] == "no_speech"
    assert {"phone_number", "name"}.isdisjoint(metadata[0])

    blind = tmp_path / "blind"
    monkeypatch.setattr("sys.argv", ["export_calibration_clips", str(blind), "--blind"])
    export()
    with (blind / "clips.csv").open() as handle:
        (rater_clip,) = list(csv.DictReader(handle))
    assert set(rater_clip) == {"clip_id", "wav_path"}
    assert (blind / rater_clip["wav_path"]).is_file()
    assert "P0001" not in rater_clip["wav_path"]
    with (tmp_path / "blind.key.csv").open() as handle:
        (key,) = list(csv.DictReader(handle))
    assert key == {"rater_clip_id": rater_clip["clip_id"], "clip_id": metadata[0]["clip_id"]}


def test_additive_migration_round_trip(clean_db):
    config = Config("alembic.ini")
    try:
        command.downgrade(config, "6ca8042a93f1")
        with db.get_sync_engine().connect() as connection:
            assert connection.scalar(text("SELECT to_regclass('calibration_clips')")) is None
        command.upgrade(config, "head")
        with db.get_sync_engine().connect() as connection:
            assert connection.scalar(text("SELECT to_regclass('calibration_clips')")) == "calibration_clips"
    finally:
        command.upgrade(config, "head")
