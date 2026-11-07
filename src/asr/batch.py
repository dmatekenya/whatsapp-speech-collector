"""The ASR batch: select recordings, send them to Modal in chunks, store the transcripts.

Step 1 of 2: transcripts only. Step 2 (scoring against the prompt text, QC
results and the report) runs after a batch, from after_batch() below.
"""

import json
import logging
import math
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from src.asr.client import Chunk, Transcriber
from src.storage.files import FileStorage
from src.storage.models import AsrBatch, Participant, Recording, Transcript

logger = logging.getLogger(__name__)

# qc_reason of clips the arrival checks rejected: never worth transcribing.
ARRIVAL_REJECTIONS = ("empty", "too_short", "too_long", "no_speech", "not_recorded")
LOCK_KEY = "asr_batch"
ERROR_LIMIT = 2000

# Why a candidate recording isn't sent (dry-run and summary counts).
REJECTED = "rejected at arrival"
DUPLICATE = "duplicate"
TEST = "test participant"
DONE = "already transcribed"
MISSING = "WAV file missing"
OVER_LIMIT = "over --limit"


class BatchLocked(RuntimeError):
    """Another batch is running (the advisory lock is held)."""


# =========
# SELECTION
# =========
@dataclass(frozen=True)
class Filters:
    model_key: str
    since: date | None = None
    until: date | None = None  # inclusive
    participants: tuple[str, ...] = ()
    recording_ids: tuple[int, ...] = ()
    include_test: bool = False
    force: bool = False
    limit: int | None = None

    def as_json(self) -> dict[str, Any]:
        """For asr_batches.filters: the options that were set."""
        data = asdict(self)
        data.pop("model_key")
        data = {k: (v.isoformat() if isinstance(v, date) else list(v) if isinstance(v, tuple) else v) for k, v in data.items()}
        return {k: v for k, v in data.items() if v not in (None, [], False)}


@dataclass(frozen=True)
class Clip:
    recording_id: int
    path: Path
    size_bytes: int
    duration_s: float


@dataclass
class Selection:
    clips: list[Clip] = field(default_factory=list)
    excluded: Counter = field(default_factory=Counter)
    missing_ids: list[int] = field(default_factory=list)

    @property
    def audio_minutes(self) -> float:
        return sum(c.duration_s for c in self.clips) / 60

    @property
    def size_mb(self) -> float:
        return sum(c.size_bytes for c in self.clips) / (1024 * 1024)


def _local_midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=tz)


def select_clips(db: Session, filters: Filters, storage: FileStorage, tz: ZoneInfo) -> Selection:
    """Stored recordings of registered participants with a WAV, oldest first, minus:
    clips rejected at arrival, duplicates, test participants (unless
    include_test), ones this model has transcribed (unless force) and missing files.
    --since/--until are local dates (tz) of when the recording was stored.
    """
    query = (
        select(
            Recording.id,
            Recording.wav_path,
            Recording.duration_s,
            Recording.qc_status,
            Recording.qc_reason,
            Recording.duplicate_of_recording_id,
            Participant.is_test,
        )
        .join(Participant, Participant.participant_id == Recording.participant_id)
        .where(Recording.status == "stored", Recording.wav_path.is_not(None))
        .order_by(Recording.id)
    )
    if filters.participants:
        query = query.where(Recording.participant_id.in_(filters.participants))
    if filters.recording_ids:
        query = query.where(Recording.id.in_(filters.recording_ids))
    if filters.since:
        query = query.where(Recording.created_at >= _local_midnight(filters.since, tz))
    if filters.until:
        query = query.where(Recording.created_at < _local_midnight(filters.until + timedelta(days=1), tz))
    done: set[int] = set()
    if not filters.force:
        done = set(
            db.scalars(
                select(Transcript.recording_id).where(
                    Transcript.source == "asr", Transcript.model_key == filters.model_key
                )
            )
        )

    selection = Selection()
    for row in db.execute(query):
        if row.qc_status == "rejected" and row.qc_reason in ARRIVAL_REJECTIONS:
            reason = REJECTED
        elif row.duplicate_of_recording_id is not None:
            reason = DUPLICATE
        elif row.is_test and not filters.include_test:
            reason = TEST
        elif row.id in done:
            reason = DONE
        elif filters.limit is not None and len(selection.clips) >= filters.limit:
            reason = OVER_LIMIT
        elif not storage.exists(row.wav_path):
            reason = MISSING
            selection.missing_ids.append(row.id)
        else:
            path = storage.absolute_path(row.wav_path)
            selection.clips.append(Clip(row.id, path, path.stat().st_size, row.duration_s or 0.0))
            continue
        selection.excluded[reason] += 1
    return selection


# ========
# CHUNKING
# ========
def chunk_clips(clips: list[Clip], max_clips: int, max_bytes: int) -> list[list[Clip]]:
    """In order, at most max_clips and max_bytes per chunk (a clip larger than max_bytes goes alone)."""
    chunks: list[list[Clip]] = []
    current: list[Clip] = []
    size = 0
    for clip in clips:
        if current and (len(current) >= max_clips or size + clip.size_bytes > max_bytes):
            chunks.append(current)
            current, size = [], 0
        current.append(clip)
        size += clip.size_bytes
    if current:
        chunks.append(current)
    return chunks


# ====
# LOCK
# ====
@contextmanager
def batch_lock(engine: Engine) -> Iterator[None]:
    """One batch at a time, across containers: a session-level advisory lock on
    its own connection, held for the whole batch. Raises BatchLocked if held."""
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        key = {"key": LOCK_KEY}
        if not conn.execute(text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"), key).scalar():
            raise BatchLocked("another ASR batch is running; try again when it has finished")
        try:
            yield
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"), key)


# =======
# RUNNING
# =======
@dataclass
class BatchReport:
    batch_id: int
    status: str
    clips_selected: int
    clips_transcribed: int
    clips_failed: int
    clips_missing: int
    audio_seconds: float
    wall_seconds: float
    gpu: str | None
    model_key: str
    model_repo: str | None
    model_revision: str | None
    failed_ids: list[int]
    error: str | None = None


def _clean(value: Any) -> Any:
    """For JSON: NaN and infinity become null."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    return value


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:ERROR_LIMIT]


def mark_interrupted(db: Session, now: datetime) -> int:
    """Batches still 'running' when a new one starts (under the lock) never finished."""
    result = db.execute(
        update(AsrBatch)
        .where(AsrBatch.status == "running")
        .values(status="interrupted", finished_at=now, error="never finished (the process stopped)")
    )
    return result.rowcount


def ran_in_slot(db: Session, slot_start: datetime) -> bool:
    return (
        db.scalar(
            select(AsrBatch.id).where(AsrBatch.trigger == "scheduled", AsrBatch.started_at >= slot_start).limit(1)
        )
        is not None
    )


def after_batch(session_factory: sessionmaker[Session], batch_id: int) -> None:
    """Hook for step 2: score the new transcripts against the prompt text (CER/WER),
    suggest bands, write qc_results and the report. Nothing yet."""


def _payloads(chunks: list[list[Clip]], unreadable: dict[int, str]) -> Iterator[Chunk]:
    """Each chunk's (recording_id, WAV bytes), read only when Modal asks for it."""
    for chunk in chunks:
        payload: Chunk = []
        for clip in chunk:
            try:
                payload.append((clip.recording_id, clip.path.read_bytes()))
            except OSError as exc:
                unreadable[clip.recording_id] = _error_text(exc)
        yield payload


@contextmanager
def _closing(results: Iterator[Any]) -> Iterator[None]:
    """Close the results iterator on the way out, if it can be closed (generators can)."""
    try:
        yield
    finally:
        close = getattr(results, "close", None)
        if close is not None:
            close()


def _no_results() -> Iterator[dict[str, Any] | BaseException]:
    """No chunks: nothing to call Modal for (a generator, so it can be closed like the real results)."""
    yield from ()


def _final_status(transcribed: int, failed: int) -> str:
    if failed == 0:
        return "completed"
    return "partial" if transcribed else "failed"


def run_batch(
    session_factory: sessionmaker[Session],
    transcriber: Transcriber,
    selection: Selection,
    filters: Filters,
    trigger: str,
    results_root: Path,
    max_clips: int,
    max_bytes: int,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> BatchReport:
    """Send the selected clips and store what comes back. Each chunk's transcripts
    and the batch counters are committed together, so a crash keeps what was
    done; failed clips get no transcript, so the next run retries them.
    Call under batch_lock()."""
    started = time.monotonic()
    with session_factory() as db:
        batch = AsrBatch(
            started_at=now(),
            status="running",
            trigger=trigger,
            model_key=filters.model_key,
            filters=filters.as_json(),
            clips_selected=len(selection.clips),
            clips_missing=len(selection.missing_ids),
        )
        db.add(batch)
        db.commit()
        batch_id = batch.id
    if selection.missing_ids:
        logger.warning("Batch %s: WAV missing for recording ids %s", batch_id, selection.missing_ids)

    out_dir = results_root / str(batch_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks = chunk_clips(selection.clips, max_clips, max_bytes)
    unreadable: dict[int, str] = {}
    failed_ids: list[int] = []
    totals = {"transcribed": 0, "failed": 0, "audio_seconds": 0.0}
    model: dict[str, Any] = {}
    gpu: str | None = None
    status: str | None = None
    error: str | None = None

    try:
        results = transcriber.transcribe_chunks(filters.model_key, _payloads(chunks, unreadable)) if chunks else _no_results()
        # Read the results to the end, never stopping at the last chunk: Modal's
        # map iterator only shuts down cleanly once it is exhausted (one left
        # suspended is closed when garbage-collected, and logs "aclose():
        # asynchronous generator is already running"). _closing(): if the batch
        # stops early (an error), close it now rather than at garbage collection.
        with _closing(results), (out_dir / "results.jsonl").open("a", encoding="utf-8") as raw:
            for number, result in enumerate(results, start=1):
                if number > len(chunks):
                    logger.warning("Batch %s: Modal returned more results than chunks sent; extra ignored", batch_id)
                    continue
                chunk = chunks[number - 1]
                ids = [c.recording_id for c in chunk]
                line: dict[str, Any] = {"batch_id": batch_id, "chunk": number, "recording_ids": ids}
                transcripts: list[Transcript] = []
                chunk_failed: list[int] = []
                if isinstance(result, BaseException):
                    line["error"] = _error_text(result)
                    chunk_failed = ids
                    logger.warning("Batch %s chunk %s failed (%s): recording ids %s", batch_id, number, type(result).__name__, ids)
                elif result.get("model", {}).get("key") != filters.model_key:
                    line["error"] = f"wrong model in the result: {result.get('model')}"
                    line["result"] = result
                    chunk_failed = ids
                    logger.warning("Batch %s chunk %s: %s", batch_id, number, line["error"])
                else:
                    line["result"] = result
                    model = model or result["model"]
                    gpu = gpu or result.get("gpu")
                    by_id = {clip.get("recording_id"): clip for clip in result.get("clips", [])}
                    for recording_id in ids:
                        clip = by_id.get(recording_id)
                        if clip is None or clip.get("error") or clip.get("text") is None:
                            chunk_failed.append(recording_id)
                            continue
                        logprob = clip.get("avg_logprob")
                        transcripts.append(
                            Transcript(
                                recording_id=recording_id,
                                source="asr",
                                model_key=filters.model_key,
                                model_repo=result["model"].get("repo"),
                                model_revision=result["model"].get("revision"),
                                text=clip["text"],
                                avg_logprob=logprob if isinstance(logprob, int | float) and math.isfinite(logprob) else None,
                                batch_id=batch_id,
                            )
                        )
                        totals["audio_seconds"] += float(clip.get("audio_seconds") or 0)
                    if chunk_failed:
                        logger.warning("Batch %s chunk %s: no transcript for recording ids %s", batch_id, number, chunk_failed)
                if unreadable:
                    line["unreadable"] = sorted(unreadable)
                    unreadable.clear()
                raw.write(json.dumps(_clean(line), ensure_ascii=False) + "\n")
                raw.flush()

                totals["transcribed"] += len(transcripts)
                totals["failed"] += len(chunk_failed)
                failed_ids += chunk_failed
                with session_factory() as db:
                    db.add_all(transcripts)
                    db.execute(
                        update(AsrBatch)
                        .where(AsrBatch.id == batch_id)
                        .values(
                            clips_transcribed=totals["transcribed"],
                            clips_failed=totals["failed"],
                            audio_seconds=totals["audio_seconds"],
                            model_repo=model.get("repo"),
                            model_revision=model.get("revision"),
                            gpu=gpu,
                        )
                    )
                    db.commit()
                logger.info(
                    "Batch %s chunk %s/%s: %s transcribed, %s failed",
                    batch_id, number, len(chunks), len(transcripts), len(chunk_failed),
                )  # fmt: skip
        # Chunks that never came back (the results stopped early).
        missing_results = len(selection.clips) - totals["transcribed"] - totals["failed"]
        if missing_results > 0:
            totals["failed"] += missing_results
            error = f"{missing_results} clip(s) got no result from Modal"
        status = _final_status(totals["transcribed"], totals["failed"])
    except (KeyboardInterrupt, SystemExit):
        status, error = "interrupted", "stopped before the end"
        raise
    except Exception as exc:  # Modal unreachable, app not deployed, ...
        logger.exception("Batch %s failed", batch_id)
        status, error = ("partial" if totals["transcribed"] else "failed"), _error_text(exc)
        totals["failed"] = len(selection.clips) - totals["transcribed"]
    finally:
        with session_factory() as db:
            db.execute(
                update(AsrBatch)
                .where(AsrBatch.id == batch_id)
                .values(
                    status=status or "interrupted",
                    finished_at=now(),
                    clips_transcribed=totals["transcribed"],
                    clips_failed=totals["failed"],
                    audio_seconds=totals["audio_seconds"],
                    error=error,
                )
            )
            db.commit()

    after_batch(session_factory, batch_id)
    return BatchReport(
        batch_id=batch_id,
        status=status,
        clips_selected=len(selection.clips),
        clips_transcribed=totals["transcribed"],
        clips_failed=totals["failed"],
        clips_missing=len(selection.missing_ids),
        audio_seconds=totals["audio_seconds"],
        wall_seconds=time.monotonic() - started,
        gpu=gpu,
        model_key=filters.model_key,
        model_repo=model.get("repo"),
        model_revision=model.get("revision"),
        failed_ids=failed_ids,
        error=error,
    )
