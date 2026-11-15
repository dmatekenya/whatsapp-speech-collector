"""Tier-2 scoring against the test database: selection, writes, integrity, the ASR hook, the script."""

import logging
import wave
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import delete, inspect, select, text, update
from sqlalchemy.exc import IntegrityError

from src.asr.batch import after_batch
from src.qc import scoring
from src.qc.levels import ALL_OK, load_qc_config
from src.qc.scoring import (
    Filters,
    run_scoring,
    score_after_batch,
    scoring_lock,
    stored_rows,
)
from src.storage import db
from src.storage.files import LocalFileStorage
from src.storage.models import (
    AsrBatch,
    Assignment,
    InboundMessage,
    Participant,
    Prompt,
    QcResult,
    Recording,
    TaskSession,
    Transcript,
)
from tests.conftest import add_participant

CONFIG = load_qc_config()
T0 = datetime(2026, 11, 9, 8, 0, tzinfo=UTC)
SENTENCE = "Ana amapita kusukulu m'mawa uliwonse ndi abwenzi awo"


# =======
# HELPERS
# =======
@pytest.fixture
def storage(tmp_path: Path) -> LocalFileStorage:
    return LocalFileStorage(tmp_path / "audio")


@pytest.fixture
def people(clean_db: None) -> None:
    add_participant("P0001", "+265991000001", name="Chisomo Banda")
    add_participant("P0002", "+265991000002", name="Ben Phiri")
    add_participant("P0009", "+265991000009", name="Tess Tester")
    with db.get_sync_sessionmaker()() as session:
        session.execute(update(Participant).where(Participant.participant_id == "P0009").values(is_test=True))
        session.commit()


def write_wav(storage: LocalFileStorage, relative: str, noise: float = 0.0) -> None:
    # Speech-like amplitudes (gamma, shape 0.4, as WADA-SNR assumes), peak 0.9, plus optional noise.
    rng = np.random.default_rng(3)
    speech = rng.gamma(0.4, 1.0, 16000 * 3) * rng.choice([-1.0, 1.0], 16000 * 3)
    signal = 0.9 * speech / np.abs(speech).max() + rng.normal(0, noise, speech.size)
    path = storage.absolute_path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes((np.clip(signal, -1, 1) * 32767).astype("<i2").tobytes())


def clip(
    storage: LocalFileStorage | None,
    participant_id: str = "P0001",
    task_type: str = "sentence",
    prompt_text: str | None = SENTENCE,
    transcript: str | None = SENTENCE.lower(),
    prompt_id: str | None = None,
    assigned: bool = True,
    model: str = "whisper_b",
    avg_logprob: float | None = -0.2,
    duration_s: float = 6.0,
    active_seconds: float | None = 4.0,
    mean_dbfs: float | None = -25.0,
    **columns: object,
) -> int:
    """One recording (with its assignment and an ASR transcript unless transcript is None)."""
    with db.get_sync_sessionmaker()() as session:
        n = session.query(InboundMessage).count() + 1
        inbound = InboundMessage(message_sid=f"wamid.Q{n}", sender="+265991000001", message_type="audio",
                                 raw_payload={}, status="processed")  # fmt: skip
        session.add(inbound)
        session.flush()
        assignment_id = None
        if assigned:
            if prompt_id and session.scalar(select(Prompt).where(Prompt.prompt_id == prompt_id)) is None:
                session.add(Prompt(prompt_id=prompt_id, prompt_type=task_type, text=prompt_text, source="test",
                                   source_file="test.csv"))  # fmt: skip
            task_session = TaskSession(participant_id=participant_id, task_type=task_type, status="completed",
                                       size=10, started_at=T0, ended_at=T0)  # fmt: skip
            session.add(task_session)
            session.flush()
            assignment = Assignment(
                participant_id=participant_id, prompt_id=prompt_id, prompt_type=task_type, session_id=task_session.id,
                position=1, status="recorded", expires_at=T0 + timedelta(days=1),
                prompt_text_snapshot=prompt_text if task_type != "free_speech" else None,
            )  # fmt: skip
            session.add(assignment)
            session.flush()
            assignment_id = assignment.id
        wav_path = f"{participant_id}/R{n:06d}.wav"
        recording = Recording(
            inbound_message_id=inbound.id, participant_id=participant_id, assignment_id=assignment_id,
            status=columns.pop("status", "stored"), wav_path=wav_path, duration_s=duration_s,
            active_seconds=active_seconds, mean_dbfs=mean_dbfs, qc_status=columns.pop("qc_status", "accepted"),
            **columns,
        )  # fmt: skip
        session.add(recording)
        session.flush()
        if transcript is not None:
            session.add(Transcript(recording_id=recording.id, source="asr", model_key=model, text=transcript,
                                   avg_logprob=avg_logprob, created_at=T0))  # fmt: skip
        session.commit()
        recording_id = recording.id
    if storage is not None:
        write_wav(storage, wav_path)
    return recording_id


def recording(recording_id: int) -> Recording:
    with db.get_sync_sessionmaker()() as session:
        return session.get(Recording, recording_id)


def results(recording_id: int | None = None) -> list[QcResult]:
    with db.get_sync_sessionmaker()() as session:
        query = select(QcResult).order_by(QcResult.id)
        if recording_id is not None:
            query = query.where(QcResult.recording_id == recording_id)
        return list(session.scalars(query))


def score(storage: LocalFileStorage, **filters: object) -> scoring.RunResult:
    return run_scoring(db.get_sync_sessionmaker(), CONFIG, Filters(**filters), storage=storage)


# =========
# SELECTION
# =========
def test_selection_skips_rejected_test_untranscribed_and_unassigned(people: None, storage: LocalFileStorage) -> None:
    ok = clip(storage)
    clip(storage, qc_status="rejected", qc_reason="too_short")
    tester = clip(storage, participant_id="P0009")
    clip(storage, transcript=None)  # not transcribed yet
    clip(storage, assigned=False)  # outside a session: never scored
    clip(storage, model="whisper_v1")  # another model's transcript only
    clip(storage, status="failed")

    assert [s.item.recording_id for s in score(storage).scored] == [ok]
    assert [s.item.recording_id for s in score(storage, include_test=True).scored] == [tester]  # ok already scored


def test_the_latest_transcript_is_scored(people: None, storage: LocalFileStorage) -> None:
    rid = clip(storage, transcript="kwathunthu kosiyana")
    with db.get_sync_sessionmaker()() as session:
        later = Transcript(recording_id=rid, source="asr", model_key="whisper_b", text=SENTENCE, created_at=T0 + timedelta(hours=1))
        same_time_lower_id = Transcript(recording_id=rid, source="asr", model_key="whisper_b", text="x", created_at=T0 + timedelta(hours=2))
        same_time_higher_id = Transcript(recording_id=rid, source="asr", model_key="whisper_b", text=SENTENCE, created_at=T0 + timedelta(hours=2))
        session.add_all([later, same_time_lower_id])
        session.flush()
        session.add(same_time_higher_id)
        session.commit()
        expected = same_time_higher_id.id

    (scored,) = score(storage).scored
    assert scored.item.transcript_id == expected and scored.metrics["cer"] == 0.0


def test_default_skips_scored_clips_and_rescore_adds_rows(people: None, storage: LocalFileStorage) -> None:
    rid = clip(storage)
    assert len(score(storage).scored) == 1
    assert score(storage).scored == []  # already scored for this thresholds_version
    assert len(score(storage, rescore=True).scored) == 1
    assert len(results(rid)) == 2  # old rows kept

    # A new thresholds_version: the default picks it up again.
    newer = CONFIG.model_copy(update={"thresholds_version": "2026-12-01"})
    assert len(run_scoring(db.get_sync_sessionmaker(), newer, Filters(), storage=storage).scored) == 1


# ======
# WRITES
# ======
def test_what_a_run_writes(people: None, storage: LocalFileStorage) -> None:
    rid = clip(storage, prompt_id="S00001", qc_reason=None)
    noisy = clip(storage, transcript="ana amapita kusukulu", task_type="sentence")  # stopped early
    before = recording(rid)

    score(storage)

    (row,) = results(rid)
    after = recording(rid)
    assert (row.band, row.band_reason, row.stage) == (4, ALL_OK, "asr")
    assert (row.metrics_version, row.thresholds_version) == ("1", CONFIG.thresholds_version)
    assert row.cer == 0.0 and row.wer == 0.0 and row.length_ratio == pytest.approx(1.0) and row.has_digits is False
    assert row.word_count == 8 and row.words_per_second == pytest.approx(8 / 4.0)
    assert after.qc_tier2 == 4 and after.snr_db is not None and after.snr_db > 20
    # Tier 2 never touches tier 1. The recordings trigger sets qc_final and, as qc_tier2
    # changed, bumps updated_at (the supervision gateway's incremental sync reads it).
    assert (after.qc_status, after.qc_reason) == (before.qc_status, before.qc_reason)
    assert (before.qc_final, after.qc_final) == ("bronze", "silver") and after.updated_at > before.updated_at

    (short,) = results(noisy)
    assert short.band == 1 and short.band_reason == "cer:1" and short.length_ratio < 0.5
    assert (recording(noisy).qc_tier2, recording(noisy).qc_final) == (1, "bronze")

    # Re-scoring to the same level leaves updated_at alone (nothing a sync needs to see).
    unchanged = recording(rid).updated_at
    score(storage, rescore=True)
    assert recording(rid).updated_at == unchanged and len(results(rid)) == 2


def test_snr_is_computed_once(people: None, storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch) -> None:
    rid = clip(storage)
    score(storage)
    first = recording(rid).snr_db
    monkeypatch.setattr(scoring.m, "read_wav", lambda path: (_ for _ in ()).throw(AssertionError("read again")))
    score(storage, rescore=True)
    assert recording(rid).snr_db == first and len(results(rid)) == 2


def test_noise_lowers_the_snr(people: None, storage: LocalFileStorage) -> None:
    clean, noisy = clip(storage), clip(None)
    write_wav(storage, recording(noisy).wav_path, noise=0.15)
    score(storage)
    assert recording(clean).snr_db > 20 > recording(noisy).snr_db


def test_missing_wav_scores_without_snr(people: None, storage: LocalFileStorage) -> None:
    rid = clip(None)  # no WAV on disk
    (scored,) = score(storage).scored
    assert scored.metrics["snr_db"] is None and recording(rid).snr_db is None and recording(rid).qc_tier2 == 4


def test_spontaneous_types_have_no_sentence_metrics(people: None, storage: LocalFileStorage) -> None:
    image = clip(storage, task_type="image", prompt_text=None, transcript="Ndikuona ana")
    free = clip(storage, task_type="free_speech", prompt_text=None, transcript="", avg_logprob=None)
    score(storage)
    (row,) = results(image)
    assert (row.cer, row.wer, row.length_ratio, row.has_digits) == (None, None, None, None)
    # Every metric is stored, but there is no level for these task types.
    assert (row.band, row.band_reason, row.word_count) == (None, "not_applicable", 2)
    assert row.words_per_second is not None and row.compression_ratio is not None
    (empty,) = results(free)
    assert (empty.band, empty.band_reason, empty.word_count, empty.compression_ratio) == (None, "not_applicable", 0, None)
    for rid in (image, free):
        assert (recording(rid).qc_tier2, recording(rid).qc_final) == (None, "silver")


def test_rescore_moves_spontaneous_clips_to_no_level(people: None, storage: LocalFileStorage) -> None:
    # Scored under the earlier thresholds (every task type got a level) ...
    image = clip(storage, task_type="image", prompt_text=None, transcript="Ndikuona ana awiri akusewera")
    old = CONFIG.model_copy(deep=True)
    old.thresholds_version, old.levelled_task_types = "2026-11-10-provisional", ["sentence", "image"]
    old.levels["snr_db"].sets_level = True
    run_scoring(db.get_sync_sessionmaker(), old, Filters(), storage=storage)
    assert recording(image).qc_tier2 == 4
    # ... the CER-only version leaves them without a level.
    score(storage, rescore=True)
    assert [(r.band, r.band_reason) for r in results(image)] == [(4, "all_ok"), (None, "not_applicable")]
    assert (recording(image).qc_tier2, recording(image).qc_final) == (None, "silver")


def test_band_is_empty_exactly_when_not_applicable(people: None, storage: LocalFileStorage) -> None:
    rid = clip(storage)
    tid = _transcript_id(rid)
    for band, reason in ((None, "cer:2"), (3, "not_applicable"), (5, "cer:1")):
        with pytest.raises(IntegrityError):
            _insert(recording_id=rid, transcript_id=tid, band=band, band_reason=reason)
    _insert(recording_id=rid, transcript_id=tid, band=None, band_reason="not_applicable")


def test_qc_tier2_always_equals_the_latest_rows_band(people: None, storage: LocalFileStorage) -> None:
    rid = clip(storage)
    score(storage)
    strict = CONFIG.model_copy(deep=True)
    strict.levels["cer"].bands[0].max = -1.0  # nothing reaches level 4 now: CER 0.0 is level 3
    run_scoring(db.get_sync_sessionmaker(), strict, Filters(rescore=True), storage=storage)
    rows = results(rid)
    assert [r.band for r in rows] == [4, 3] and recording(rid).qc_tier2 == 3


# =========
# INTEGRITY
# =========
def _insert(**values: object) -> None:
    defaults = {"stage": "asr", "band": 4, "band_reason": ALL_OK, "metrics_version": "1", "thresholds_version": "t"}
    with db.get_sync_sessionmaker()() as session:
        session.add(QcResult(**{**defaults, **values}))
        session.commit()


def _transcript_id(recording_id: int) -> int:
    with db.get_sync_sessionmaker()() as session:
        return session.scalar(select(Transcript.id).where(Transcript.recording_id == recording_id))


def test_a_result_for_a_missing_recording_is_refused(people: None, storage: LocalFileStorage) -> None:
    rid = clip(storage)
    with pytest.raises(IntegrityError):
        _insert(recording_id=rid + 999, transcript_id=_transcript_id(rid))


def test_a_transcript_from_another_recording_is_refused(people: None, storage: LocalFileStorage) -> None:
    first, second = clip(storage), clip(storage)
    with pytest.raises(IntegrityError, match="fk_qc_results_transcript_recording"):
        _insert(recording_id=first, transcript_id=_transcript_id(second))


def test_deleting_a_recording_or_transcript_with_results_is_refused(people: None, storage: LocalFileStorage) -> None:
    rid = clip(storage)
    score(storage)
    with db.get_sync_sessionmaker()() as session, pytest.raises(IntegrityError):
        session.execute(delete(Recording).where(Recording.id == rid))
        session.commit()
    # The transcripts key fires first above, so check qc_results' own keys too.
    with db.get_sync_sessionmaker()() as session, pytest.raises(IntegrityError, match="fk_qc_results_transcript_recording"):
        session.execute(delete(Transcript).where(Transcript.recording_id == rid))
        session.commit()
    keys = {fk["name"]: fk["options"].get("ondelete") for fk in inspect(db.get_sync_engine()).get_foreign_keys("qc_results")}
    assert keys == {"fk_qc_results_recording_id": "RESTRICT", "fk_qc_results_transcript_recording": "RESTRICT"}
    assert len(results(rid)) == 1


def test_latest_result_is_by_created_at_then_id(people: None, storage: LocalFileStorage) -> None:
    rid = clip(storage)
    tid = _transcript_id(rid)
    same = T0 + timedelta(hours=3)
    _insert(recording_id=rid, transcript_id=tid, band=3, band_reason="snr_db:3", created_at=T0, thresholds_version=CONFIG.thresholds_version)
    _insert(recording_id=rid, transcript_id=tid, band=2, band_reason="cer:2", created_at=same, thresholds_version=CONFIG.thresholds_version)
    _insert(recording_id=rid, transcript_id=tid, band=1, band_reason="cer:1", created_at=same, thresholds_version=CONFIG.thresholds_version)
    with db.get_sync_sessionmaker()() as session:
        (row,) = stored_rows(session, CONFIG, Filters())
    assert (row.band, row.band_reason) == (1, "cer:1")  # same created_at: the higher id


def test_the_migration_downgrade_removes_exactly_what_it_added(clean_db: None) -> None:
    from alembic import command
    from alembic.config import Config

    def schema() -> dict:
        inspector = inspect(db.get_sync_engine())
        return {
            "tables": set(inspector.get_table_names()),
            "recordings": {c["name"] for c in inspector.get_columns("recordings")},
            "checks": {c["name"] for c in inspector.get_check_constraints("recordings")},
            "transcript_uniques": {u["name"] for u in inspector.get_unique_constraints("transcripts")},
            "routines": routines(),
        }

    def routines() -> set[str]:
        with db.get_sync_engine().connect() as conn:
            functions = conn.execute(text("SELECT proname FROM pg_proc WHERE pronamespace = 'public'::regnamespace"))
            triggers = conn.execute(text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"))
            return {row[0] for row in functions} | {row[0] for row in triggers}

    after = schema()
    command.downgrade(Config("alembic.ini"), "f3a7c2d9e816")  # tier 2 (two migrations) and tier 3
    try:
        before = schema()
    finally:
        command.upgrade(Config("alembic.ini"), "head")
    assert after["tables"] - before["tables"] == {"qc_results", "qc_human_reviews"}
    assert after["recordings"] - before["recordings"] == {"snr_db", "qc_tier2", "qc_tier3", "qc_tier3_reason", "qc_final"}
    assert after["checks"] - before["checks"] == {"ck_recordings_qc_tier2", "ck_recordings_qc_tier3", "ck_recordings_qc_final"}
    assert after["transcript_uniques"] - before["transcript_uniques"] == {"uq_transcripts_id_recording_id"}
    assert after["routines"] - before["routines"] == {
        "qc_final_label", "recordings_set_qc_final", "qc_human_reviews_apply", "qc_human_reviews_append_only",
        "trg_recordings_qc_final", "trg_qc_human_reviews_apply", "trg_qc_human_reviews_append_only",
    }  # fmt: skip
    assert schema() == after


# ===============
# AFTER ASR BATCH
# ===============
def _batch(include_test: bool = False) -> int:
    with db.get_sync_sessionmaker()() as session:
        batch = AsrBatch(started_at=T0, status="completed", trigger="manual", model_key="whisper_b",
                         filters={"include_test": include_test})  # fmt: skip
        session.add(batch)
        session.commit()
        return batch.id


def test_after_batch_scores_what_the_batch_transcribed(people: None, storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scoring, "get_audio_storage", lambda: storage)
    earlier = clip(storage)
    in_batch = clip(storage)
    batch_id = _batch()
    with db.get_sync_sessionmaker()() as session:
        session.execute(update(Transcript).where(Transcript.recording_id == in_batch).values(batch_id=batch_id))
        session.commit()

    after_batch(db.get_sync_sessionmaker(), batch_id)

    assert recording(in_batch).qc_tier2 == 4 and recording(earlier).qc_tier2 is None


def test_a_scoring_failure_does_not_fail_the_asr_batch(
    people: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(session_factory, batch_id):
        raise RuntimeError("database went away")

    monkeypatch.setattr(scoring, "score_after_batch", broken)
    with caplog.at_level(logging.ERROR, logger="src.asr.batch"):
        after_batch(db.get_sync_sessionmaker(), 7)  # doesn't raise
    assert "tier-2 scoring failed (RuntimeError)" in caplog.text


def test_after_batch_skips_while_another_scoring_run_holds_the_lock(people: None, storage: LocalFileStorage) -> None:
    rid = clip(storage)
    batch_id = _batch()
    with db.get_sync_sessionmaker()() as session:
        session.execute(update(Transcript).where(Transcript.recording_id == rid).values(batch_id=batch_id))
        session.commit()
    with scoring_lock(db.get_sync_engine()):
        assert score_after_batch(db.get_sync_sessionmaker(), batch_id) is None
    assert recording(rid).qc_tier2 is None  # picked up by the next run


# ===================
# DRY RUN AND SCRIPT
# ===================
def test_dry_run_writes_nothing(people: None, storage: LocalFileStorage) -> None:
    rid = clip(storage)
    result = run_scoring(db.get_sync_sessionmaker(), CONFIG, Filters(), dry_run=True, storage=storage)
    assert len(result.scored) == 1 and result.scored[0].band == 4
    assert results() == [] and recording(rid).qc_tier2 is None and recording(rid).snr_db is None


def test_script_report_and_sample(
    people: None, storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    import scripts.score_tier2 as cli

    monkeypatch.setattr(scoring, "get_audio_storage", lambda: storage)
    monkeypatch.setattr(cli, "SAMPLE_DIR", tmp_path / "qc")
    add_participant("P0003", "+265991000003")
    for person in ("P0001", "P0002", "P0003"):  # three readings of one sentence
        clip(storage, participant_id=person, prompt_id="S00001")
    clip(storage, prompt_id="S00002", transcript="kwathunthu kosiyana")
    clip(storage, task_type="image", prompt_text=None, transcript="Ndikuona ana awiri akusewera mpira pabwalo")
    clip(storage, transcript=None)  # eligible, but not transcribed yet

    assert cli.main(["--dry-run"]) == 0
    assert results() == []
    assert cli.main([]) == 0
    report_file = tmp_path / "reports" / "tier2.txt"
    assert cli.main(["--report-only", "--sample", "2", "--report-file", str(report_file)]) == 0

    out = capsys.readouterr().out
    assert "Clips per task type and level" in out and "S00002     median CER" in out
    assert "still without a transcript (waiting for the ASR batch): 1" in out
    image_row = next(line for line in out.splitlines() if line.strip().startswith("image"))
    assert image_row.split()[1:5] == ["-", "-", "-", "-"] and "1 / " in image_row  # n/a, not levels
    saved = report_file.read_text()
    assert "Tier-2 report (stored results" in saved and "Listening sample" in saved and "Chisomo" not in saved
    assert "P0001" in out and "Chisomo" not in out and "kwathunthu" not in out and "+265" not in out
    sample = (tmp_path / "qc" / f"tier2-sample-{CONFIG.thresholds_version}.csv").read_text().splitlines()
    assert sample[0].startswith("recording_id,participant_id,task_type,prompt_id,level,band_reason")
    assert len(sample) == 1 + 2 + 1 + 1  # sentence L4 (2 of 3), sentence L1, image n/a
    assert any(",image,," in line and ",n/a,not_applicable," in line for line in sample)
    assert "kwathunthu" not in "".join(sample)


def test_script_since_and_participants(people: None, storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.score_tier2 as cli

    monkeypatch.setattr(scoring, "get_audio_storage", lambda: storage)
    clip(storage, participant_id="P0001")
    other = clip(storage, participant_id="P0002")
    assert cli.main(["--participants", "p0002", "--since", (date.today() - timedelta(days=1)).isoformat()]) == 0
    assert [r.recording_id for r in results()] == [other]
