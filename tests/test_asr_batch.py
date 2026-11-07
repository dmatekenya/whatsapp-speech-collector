"""ASR batch, step 1: config, schedule slots, selection, chunking, and a run with a fake Modal."""

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

import scripts.asr_batch as cli
from src.asr.batch import (
    DONE,
    DUPLICATE,
    MISSING,
    OVER_LIMIT,
    REJECTED,
    TEST,
    Clip,
    Filters,
    batch_lock,
    chunk_clips,
    run_batch,
    select_clips,
)
from src.asr.config import (
    MODEL_KEYS,
    AsrBatchConfig,
    AsrConfigError,
    Schedule,
    load_asr_config,
)
from src.storage import db
from src.storage.files import LocalFileStorage
from src.storage.models import (
    AsrBatch,
    InboundMessage,
    Participant,
    Recording,
    Transcript,
)
from tests.conftest import add_participant

BLANTYRE = Schedule(runs_per_day=2, first_run="02:00", timezone="Africa/Blantyre").tz
REAL_CONFIG = Path(__file__).resolve().parents[1] / "config" / "asr_batch.yaml"


def local(*args: int) -> datetime:
    return datetime(*args, tzinfo=BLANTYRE)


# ======
# CONFIG
# ======
def write_config(tmp_path: Path, **changes: Any) -> Path:
    data = {
        "schedule": {"runs_per_day": 2, "first_run": "02:00", "timezone": "Africa/Blantyre"},
        "modal": {"app_name": "tiyankhuleni-asr", "class_name": "Transcriber", "model": "whisper_b", "chunk_clips": 30, "chunk_max_mb": 20},
        "selection": {"include_test": False},
        "results_dir": str(tmp_path / "results"),
    }
    for path, value in changes.items():
        section, key = path.split("__")
        data[section][key] = value
    target = tmp_path / "asr_batch.yaml"
    target.write_text(json.dumps(data))  # JSON is YAML
    return target


def test_the_real_config_loads() -> None:
    config = load_asr_config(REAL_CONFIG)
    assert config.schedule.runs_per_day == 2 and config.schedule.first_run == "02:00"
    assert config.modal.model == "whisper_b" and config.modal.chunk_max_bytes == 20 * 1024 * 1024
    assert config.selection.include_test is False and str(config.results_dir) == "data/asr_batches"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"schedule__runs_per_day": 5}, "divide 24"),
        ({"schedule__runs_per_day": 48}, "runs_per_day"),
        ({"schedule__runs_per_day": -1}, "runs_per_day"),
        ({"schedule__first_run": "2:00"}, "24-hour time"),
        ({"schedule__first_run": "24:00"}, "24-hour time"),
        ({"schedule__first_run": "02:60"}, "24-hour time"),
        ({"schedule__timezone": "Africa/Lilongwe_Town"}, "unknown timezone"),
        ({"modal__model": "mms"}, "unknown model 'mms'"),
        ({"modal__chunk_clips": 0}, "chunk_clips"),
        ({"selection__include_tests": True}, "include_tests"),
    ],
)
def test_bad_config_is_refused_clearly(tmp_path: Path, change: dict, message: str) -> None:
    with pytest.raises(AsrConfigError, match=message):
        load_asr_config(write_config(tmp_path, **change))


def test_model_keys_match_the_modal_app() -> None:
    from scripts.modal_asr_app import DEFAULT_MODEL, MODELS

    assert tuple(MODELS) == MODEL_KEYS and DEFAULT_MODEL in MODEL_KEYS


# =====
# SLOTS
# =====
@pytest.mark.parametrize(
    ("runs", "first", "expected"),
    [
        (1, "02:00", ["02:00"]),
        (2, "02:00", ["02:00", "14:00"]),
        (3, "02:00", ["02:00", "10:00", "18:00"]),
        (2, "20:30", ["08:30", "20:30"]),
        (0, "02:00", []),
    ],
)
def test_slot_times(runs: int, first: str, expected: list[str]) -> None:
    schedule = Schedule(runs_per_day=runs, first_run=first, timezone="Africa/Blantyre")
    assert [t.strftime("%H:%M") for t in schedule.slot_times()] == expected


def test_24_runs_a_day_is_every_hour() -> None:
    schedule = Schedule(runs_per_day=24, first_run="00:00", timezone="UTC")
    assert schedule.slot_times() == [time(h) for h in range(24)]


def test_current_slot_is_the_first_hour_after_a_slot_starts() -> None:
    schedule = Schedule(runs_per_day=2, first_run="02:00", timezone="Africa/Blantyre")
    assert schedule.current_slot(local(2026, 11, 6, 2, 5)) == local(2026, 11, 6, 2, 0)
    assert schedule.current_slot(local(2026, 11, 6, 2, 59)) == local(2026, 11, 6, 2, 0)
    assert schedule.current_slot(local(2026, 11, 6, 14, 5)) == local(2026, 11, 6, 14, 0)
    assert schedule.current_slot(local(2026, 11, 6, 3, 5)) is None
    assert schedule.current_slot(local(2026, 11, 6, 1, 55)) is None
    # Cron's clock may be UTC: Blantyre is UTC+2, so 00:05 UTC is the 02:00 slot.
    assert schedule.current_slot(datetime(2026, 11, 6, 0, 5, tzinfo=UTC)) == local(2026, 11, 6, 2, 0)
    assert schedule.current_slot(datetime(2026, 11, 6, 2, 5, tzinfo=UTC)) is None


def test_a_slot_late_in_the_day_runs_on_past_midnight() -> None:
    schedule = Schedule(runs_per_day=1, first_run="23:30", timezone="Africa/Blantyre")
    assert schedule.current_slot(local(2026, 11, 7, 0, 15)) == local(2026, 11, 6, 23, 30)
    assert schedule.current_slot(local(2026, 11, 7, 0, 35)) is None


def test_paused_has_no_slots() -> None:
    schedule = Schedule(runs_per_day=0, first_run="02:00", timezone="Africa/Blantyre")
    assert schedule.paused and schedule.current_slot(local(2026, 11, 6, 2, 5)) is None


# ========
# CHUNKING
# ========
def clips(*sizes: int) -> list[Clip]:
    return [Clip(n, Path(f"{n}.wav"), size, 1.0) for n, size in enumerate(sizes, start=1)]


def ids(chunks: list[list[Clip]]) -> list[list[int]]:
    return [[c.recording_id for c in chunk] for chunk in chunks]


def test_chunks_by_count() -> None:
    assert ids(chunk_clips(clips(*[10] * 7), max_clips=3, max_bytes=1000)) == [[1, 2, 3], [4, 5, 6], [7]]


def test_chunks_by_size_and_a_big_clip_goes_alone() -> None:
    chunks = chunk_clips(clips(40, 40, 30, 150, 10), max_clips=30, max_bytes=100)
    assert ids(chunks) == [[1, 2], [3], [4], [5]]


def test_no_clips_no_chunks() -> None:
    assert chunk_clips([], 30, 100) == []


# ==========
# RECORDINGS
# ==========
@pytest.fixture
def storage(tmp_path: Path) -> LocalFileStorage:
    return LocalFileStorage(tmp_path / "audio")


@pytest.fixture
def people(clean_db: None) -> None:
    add_participant("P0001", "+265991000001")
    add_participant("P0002", "+265991000002")
    add_participant("P0006", "+265991000006")
    with db.get_sync_sessionmaker()() as session:
        session.scalar(select(Participant).where(Participant.participant_id == "P0006")).is_test = True
        session.commit()


def add_recording(
    storage: LocalFileStorage,
    participant_id: str | None = "P0001",
    wav: bool = True,
    size: int = 1000,
    stored_at: datetime | None = None,
    **columns: Any,
) -> int:
    with db.get_sync_sessionmaker()() as session:
        n = session.query(InboundMessage).count() + 1
        message = InboundMessage(message_sid=f"wamid.T{n}", sender="+265991000000", message_type="audio", raw_payload={}, status="processed")
        session.add(message)
        session.flush()
        wav_path = f"{participant_id or '_unassigned'}/R{n:06d}.wav"
        recording = Recording(
            inbound_message_id=message.id,
            participant_id=participant_id,
            status=columns.pop("status", "stored"),
            wav_path=wav_path,
            duration_s=columns.pop("duration_s", 6.0),
            **columns,
        )
        if stored_at:
            recording.created_at = stored_at
        session.add(recording)
        session.commit()
        if wav:
            storage.save(wav_path, b"R" * size)
        return recording.id


def add_transcript(recording_id: int, model_key: str = "whisper_b") -> None:
    with db.get_sync_sessionmaker()() as session:
        session.add(Transcript(recording_id=recording_id, source="asr", model_key=model_key, text="earlier"))
        session.commit()


def selected(storage: LocalFileStorage, **filters: Any) -> tuple[list[int], dict[str, int], list[int]]:
    with db.get_sync_sessionmaker()() as session:
        found = select_clips(session, Filters(model_key=filters.pop("model_key", "whisper_b"), **filters), storage, BLANTYRE)
    return [c.recording_id for c in found.clips], dict(found.excluded), found.missing_ids


# =========
# SELECTION
# =========
def test_selection_rules(people: None, storage: LocalFileStorage) -> None:
    ok = add_recording(storage)
    flagged = add_recording(storage, qc_status="needs_review", qc_reason="low_speech")
    rejected = [
        add_recording(storage, qc_status="rejected", qc_reason=reason)
        for reason in ("too_short", "too_long", "empty", "no_speech", "not_recorded")
    ]
    later_rejection = add_recording(storage, qc_status="rejected", qc_reason="wrong_sentence")  # not an arrival check
    duplicate = add_recording(storage, duplicate_of_recording_id=ok)
    test_person = add_recording(storage, participant_id="P0006")
    missing = add_recording(storage, wav=False)
    add_recording(storage, participant_id=None)  # unregistered sender: never sent
    add_recording(storage, status="failed")
    done = add_recording(storage, participant_id="P0002")
    add_transcript(done, "whisper_b")

    chosen, excluded, missing_ids = selected(storage)
    assert chosen == [ok, flagged, later_rejection]
    assert excluded == {REJECTED: len(rejected), DUPLICATE: 1, TEST: 1, MISSING: 1, DONE: 1}
    assert missing_ids == [missing] and duplicate not in chosen and test_person not in chosen

    # Test participants on request; another model hasn't transcribed `done`; --force redoes it.
    assert selected(storage, include_test=True)[0] == [ok, flagged, later_rejection, test_person]
    assert done in selected(storage, model_key="whisper_v1")[0]
    assert done in selected(storage, force=True)[0]


def test_selection_filters(people: None, storage: LocalFileStorage) -> None:
    early = add_recording(storage, stored_at=local(2026, 11, 1, 23, 50))
    p2 = add_recording(storage, participant_id="P0002", stored_at=local(2026, 11, 2, 0, 10))
    late = add_recording(storage, stored_at=local(2026, 11, 3, 23, 59))
    after = add_recording(storage, stored_at=local(2026, 11, 4, 0, 1))

    # Dates are local (Blantyre) days, --until inclusive.
    assert selected(storage, since=date(2026, 11, 2), until=date(2026, 11, 3))[0] == [p2, late]
    assert selected(storage, participants=("P0002",))[0] == [p2]
    assert selected(storage, recording_ids=(early, after))[0] == [early, after]
    chosen, excluded, _ = selected(storage, limit=2)
    assert chosen == [early, p2] and excluded == {OVER_LIMIT: 2}


# =======
# RUNNING
# =======
class FakeTranscriber:
    """Stands in for Modal: one result per chunk, in order."""

    def __init__(self, fail_chunks: tuple[int, ...] = (), bad_clips: tuple[int, ...] = (), on_chunk=None) -> None:
        self.fail_chunks = fail_chunks
        self.bad_clips = bad_clips
        self.on_chunk = on_chunk
        self.calls: list[list[int]] = []

    def transcribe_chunks(self, model_key: str, chunks) -> Iterator[dict | BaseException]:
        for number, chunk in enumerate(chunks, start=1):
            self.calls.append([recording_id for recording_id, _ in chunk])
            if self.on_chunk:
                self.on_chunk(number)
            if number in self.fail_chunks:
                yield RuntimeError("GPU container lost")
                continue
            yield {
                "model": {"key": model_key, "repo": "dmatekenya/whisper-test", "revision": "abc123def456"},
                "gpu": "NVIDIA L4",
                "clips": [
                    {
                        "recording_id": recording_id,
                        "text": None if recording_id in self.bad_clips else f"moni {recording_id}",
                        "avg_logprob": None if recording_id in self.bad_clips else -0.25,
                        "audio_seconds": len(data) / 100,
                        "error": "audio: LibsndfileError: bad file" if recording_id in self.bad_clips else None,
                    }
                    for recording_id, data in chunk
                ],
            }


def run(storage: LocalFileStorage, tmp_path: Path, transcriber: FakeTranscriber, max_clips: int = 2, **filters: Any):
    with db.get_sync_sessionmaker()() as session:
        selection = select_clips(session, Filters(model_key="whisper_b", **filters), storage, BLANTYRE)
    return run_batch(
        db.get_sync_sessionmaker(), transcriber, selection, Filters(model_key="whisper_b", **filters), "manual",
        tmp_path / "results", max_clips=max_clips, max_bytes=10**9,
    )  # fmt: skip


def transcripts() -> list[Transcript]:
    with db.get_sync_sessionmaker()() as session:
        return list(session.scalars(select(Transcript).order_by(Transcript.id)))


def batch_row(batch_id: int) -> AsrBatch:
    with db.get_sync_sessionmaker()() as session:
        return session.get(AsrBatch, batch_id)


def test_a_run_writes_transcripts_per_chunk(people: None, storage: LocalFileStorage, tmp_path: Path) -> None:
    ids_ = [add_recording(storage, size=100 * n) for n in range(1, 6)]
    committed: list[int] = []

    def check(number: int) -> None:
        # When chunk n is requested, chunk n-1's transcripts are already committed.
        committed.append(len(transcripts()))

    fake = FakeTranscriber(bad_clips=(ids_[3],), on_chunk=check)
    report = run(storage, tmp_path, fake)

    assert fake.calls == [ids_[0:2], ids_[2:4], ids_[4:5]]
    assert committed == [0, 2, 3]
    rows = transcripts()
    assert [t.recording_id for t in rows] == [ids_[0], ids_[1], ids_[2], ids_[4]]  # the bad clip: none
    assert {(t.source, t.model_key, t.model_repo, t.model_revision, t.batch_id) for t in rows} == {
        ("asr", "whisper_b", "dmatekenya/whisper-test", "abc123def456", report.batch_id)
    }
    assert rows[0].text == f"moni {ids_[0]}" and rows[0].avg_logprob == -0.25

    assert (report.status, report.clips_selected, report.clips_transcribed, report.clips_failed) == ("partial", 5, 4, 1)
    assert report.failed_ids == [ids_[3]]
    row = batch_row(report.batch_id)
    assert (row.status, row.trigger, row.clips_transcribed, row.clips_failed, row.gpu) == ("partial", "manual", 4, 1, "NVIDIA L4")
    assert (row.model_repo, row.model_revision) == ("dmatekenya/whisper-test", "abc123def456")
    assert row.audio_seconds == pytest.approx((100 + 200 + 300 + 500) / 100)
    assert row.finished_at is not None and row.started_at <= row.finished_at

    lines = [json.loads(line) for line in (tmp_path / "results" / str(report.batch_id) / "results.jsonl").read_text().splitlines()]
    assert [line["chunk"] for line in lines] == [1, 2, 3]
    assert lines[1]["result"]["clips"][1]["error"].startswith("audio:")

    # The failed clip is retried next run; the others aren't sent again.
    again = run(storage, tmp_path, FakeTranscriber())
    assert again.status == "completed" and again.clips_selected == 1 and again.clips_transcribed == 1


def test_a_failed_chunk_is_counted_and_retried(people: None, storage: LocalFileStorage, tmp_path: Path) -> None:
    ids_ = [add_recording(storage) for _ in range(4)]
    report = run(storage, tmp_path, FakeTranscriber(fail_chunks=(1,)))

    assert (report.status, report.clips_transcribed, report.clips_failed) == ("partial", 2, 2)
    assert report.failed_ids == ids_[:2] and [t.recording_id for t in transcripts()] == ids_[2:]
    line = json.loads((tmp_path / "results" / str(report.batch_id) / "results.jsonl").read_text().splitlines()[0])
    assert line["error"] == "RuntimeError: GPU container lost" and line["recording_ids"] == ids_[:2]


def test_everything_failing_is_a_failed_batch(people: None, storage: LocalFileStorage, tmp_path: Path) -> None:
    add_recording(storage)

    class Unreachable:
        def transcribe_chunks(self, model_key: str, chunks):
            raise ConnectionError("Modal app tiyankhuleni-asr not found")
            yield  # a generator, like the real one

    report = run(storage, tmp_path, Unreachable())
    assert (report.status, report.clips_transcribed, report.clips_failed) == ("failed", 0, 1)
    assert "not found" in batch_row(report.batch_id).error and transcripts() == []


def test_nothing_to_do_is_a_completed_empty_batch(people: None, storage: LocalFileStorage, tmp_path: Path) -> None:
    fake = FakeTranscriber()
    report = run(storage, tmp_path, fake)
    assert report.status == "completed" and report.clips_selected == 0 and fake.calls == []


# ===
# CLI
# ===
@pytest.fixture
def cli_env(people: None, storage: LocalFileStorage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """main() with a test config and audio folder; returns a function to change the config."""

    def use(**changes: Any) -> AsrBatchConfig:
        config = load_asr_config(write_config(tmp_path, **changes))
        monkeypatch.setattr(cli, "get_asr_config", lambda: config)
        return config

    monkeypatch.setattr(cli, "get_audio_storage", lambda: storage)
    use()
    return use


def batches() -> list[AsrBatch]:
    with db.get_sync_sessionmaker()() as session:
        return list(session.scalars(select(AsrBatch).order_by(AsrBatch.id)))


def test_scheduled_runs_once_per_slot(cli_env, storage: LocalFileStorage) -> None:
    add_recording(storage)
    fake = FakeTranscriber()

    assert cli.main(["--scheduled"], fake, now=local(2026, 11, 6, 3, 5)) == 0  # not a slot hour
    assert batches() == []
    assert cli.main(["--scheduled"], fake, now=local(2026, 11, 6, 2, 5)) == 0
    assert [(b.trigger, b.status, b.clips_transcribed) for b in batches()] == [("scheduled", "completed", 1)]
    # Cron firing again in the same slot: nothing new.
    assert cli.main(["--scheduled"], fake, now=local(2026, 11, 6, 2, 35)) == 0
    assert len(batches()) == 1
    # The next slot runs (nothing left to send, but the slot is recorded).
    assert cli.main(["--scheduled"], fake, now=local(2026, 11, 6, 14, 5)) == 0
    assert len(batches()) == 2


def test_a_change_of_runs_per_day_applies_at_the_next_check(cli_env, storage: LocalFileStorage) -> None:
    add_recording(storage)
    cli_env(schedule__runs_per_day=0)
    assert cli.main(["--scheduled"], FakeTranscriber(), now=local(2026, 11, 6, 2, 5)) == 0
    assert batches() == []  # paused
    cli_env(schedule__runs_per_day=4)  # 02:00, 08:00, 14:00, 20:00
    assert cli.main(["--scheduled"], FakeTranscriber(), now=local(2026, 11, 6, 8, 5)) == 0
    assert len(batches()) == 1


def test_dry_run_saves_nothing(cli_env, storage: LocalFileStorage, capsys: pytest.CaptureFixture) -> None:
    add_recording(storage, participant_id="P0006", duration_s=90)
    fake = FakeTranscriber()

    assert cli.main(["--dry-run"], fake) == 0
    assert "Selected 0 clip(s)" in capsys.readouterr().out
    assert cli.main(["--dry-run", "--include-test", "--participant", "P0006"], fake) == 0
    out = capsys.readouterr().out
    assert "Selected 1 clip(s) for whisper_b: 1.5 audio minutes" in out
    assert fake.calls == [] and batches() == [] and transcripts() == []


def test_a_manual_run_prints_a_summary_without_personal_data(cli_env, storage: LocalFileStorage, capsys: pytest.CaptureFixture) -> None:
    recording_id = add_recording(storage)
    assert cli.main([], FakeTranscriber()) == 0
    out = capsys.readouterr().out
    assert "completed: 1 transcribed, 0 failed of 1 selected" in out and "NVIDIA L4" in out
    assert "abc123def456" in out and "+265" not in out
    assert [t.recording_id for t in transcripts()] == [recording_id]


def test_an_unfinished_batch_is_marked_interrupted(cli_env) -> None:
    with db.get_sync_sessionmaker()() as session:
        session.add(AsrBatch(started_at=datetime.now(UTC) - timedelta(hours=3), status="running", trigger="manual", model_key="whisper_b"))
        session.commit()
    assert cli.main([], FakeTranscriber()) == 0
    first, second = batches()
    assert first.status == "interrupted" and first.finished_at is not None
    assert second.status == "completed"


def test_a_second_batch_while_one_runs_exits(cli_env, storage: LocalFileStorage, capsys: pytest.CaptureFixture) -> None:
    add_recording(storage)
    with batch_lock(db.get_sync_engine()):  # as another container's batch would hold it
        assert cli.main([], FakeTranscriber()) == 3
        assert "another ASR batch is running" in capsys.readouterr().err
        assert cli.main(["--scheduled"], FakeTranscriber(), now=local(2026, 11, 6, 2, 5)) == 0
    assert batches() == []
    # Released: the next run goes ahead.
    assert cli.main([], FakeTranscriber()) == 0


def test_bad_options_are_refused(cli_env) -> None:
    for argv in (["--model", "mms"], ["--recordings", "12,abc"], ["--limit", "0"], ["--since", "2026-11-05", "--until", "2026-11-01"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(argv, FakeTranscriber())
        assert exc.value.code == 2


# ==============
# MODAL ITERATOR
# ==============
class TrackedTranscriber(FakeTranscriber):
    """Records how its results generator ended: run to the end, or closed early.

    Modal's map iterator logs "aclose(): asynchronous generator is already
    running" when it's left suspended and then closed; it must be read to the end.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.ended: str | None = None

    def transcribe_chunks(self, model_key: str, chunks) -> Iterator[dict | BaseException]:
        try:
            yield from super().transcribe_chunks(model_key, chunks)
        except GeneratorExit:
            self.ended = "closed early"
            raise
        self.ended = "exhausted"


def test_the_results_are_read_to_the_end(people: None, storage: LocalFileStorage, tmp_path: Path) -> None:
    for _ in range(5):
        add_recording(storage)
    fake = TrackedTranscriber()

    report = run(storage, tmp_path, fake)

    assert report.status == "completed" and len(fake.calls) == 3
    # Not left suspended after the last chunk's result.
    assert fake.ended == "exhausted"


def test_the_results_are_closed_at_once_when_the_batch_stops_early(
    people: None, storage: LocalFileStorage, tmp_path: Path
) -> None:
    for _ in range(4):
        add_recording(storage)

    class Malformed(TrackedTranscriber):
        def transcribe_chunks(self, model_key: str, chunks) -> Iterator[dict | BaseException]:
            try:
                for _ in chunks:
                    yield {"model": None}  # breaks the batch while results remain
                self.ended = "exhausted"
            except GeneratorExit:
                self.ended = "closed early"
                raise

    fake = Malformed()
    report = run(storage, tmp_path, fake)

    assert report.status == "failed" and transcripts() == []
    assert fake.ended == "closed early"  # by the batch itself, not later by garbage collection
