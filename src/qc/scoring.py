"""Tier-2 scoring runs: which recordings, the metrics, and what is written.

Eligible: stored recordings, not rejected at arrival (qc_status <> 'rejected'), answering an
assignment (unassigned recordings aren't scored), with an ASR transcript from the configured
model, and not from a test participant unless include_test. The transcript scored is that
model's latest for the recording (created_at, then id).

Per recording, in one transaction: recordings.snr_db if it is missing, a new qc_results row,
and recordings.qc_tier2 = that row's band (None for task types that get no level). Nothing
else: never qc_status or qc_reason, and nothing is sent to participants. The recordings
trigger then recomputes qc_final and, when qc_tier2 changed, updated_at.

Logs recording IDs and counts only: never transcripts, names or numbers.
"""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, exists, func, select, text, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session, sessionmaker

from src.qc import metrics as m
from src.qc.levels import QcConfig, clip_level, load_qc_config
from src.storage.audio import get_audio_storage
from src.storage.db import get_sync_engine
from src.storage.files import FileStorage
from src.storage.models import (
    AsrBatch,
    Assignment,
    Participant,
    QcResult,
    Recording,
    Transcript,
)

logger = logging.getLogger(__name__)

LOCK_KEY = "qc_tier2"  # not the ASR batch's key ("asr_batch"): the two never block each other
STAGE = "asr"
LOCAL_TZ = ZoneInfo("Africa/Blantyre")  # --since dates are Malawi dates, as in the ASR batch
# Values in reports and the sample, in this order.
REPORT_METRICS = (
    "duration_s", "speech_ratio", "mean_dbfs", "snr_db", "word_count", "words_per_second",
    "avg_logprob", "compression_ratio", "cer", "wer", "length_ratio",
)  # fmt: skip


class ScoringLocked(RuntimeError):
    """Another scoring run holds the lock."""


@contextmanager
def scoring_lock(engine: Engine) -> Iterator[None]:
    """One scoring run at a time, across containers (a session-level advisory lock)."""
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        key = {"key": LOCK_KEY}
        if not conn.execute(text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"), key).scalar():
            raise ScoringLocked("another tier-2 scoring run is in progress; try again when it has finished")
        try:
            yield
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"), key)


# =========
# SELECTION
# =========
@dataclass
class Filters:
    recording_ids: tuple[int, ...] = ()
    participants: tuple[str, ...] = ()
    since: date | None = None
    include_test: bool = False
    rescore: bool = False  # False: only recordings with no result for the current thresholds_version
    limit: int | None = None
    batch_id: int | None = None  # only recordings with a transcript from this ASR batch


@dataclass
class Item:
    """One recording to score, with what the metrics need."""

    recording_id: int
    participant_id: str
    task_type: str
    prompt_id: str | None
    prompt_text: str | None
    transcript_id: int
    transcript: str
    avg_logprob: float | None
    duration_s: float | None
    active_seconds: float | None
    mean_dbfs: float | None
    snr_db: float | None
    wav_path: str | None


def _latest_transcripts(model: str):
    return (
        select(
            Transcript.id.label("transcript_id"),
            Transcript.recording_id,
            Transcript.text,
            Transcript.avg_logprob,
        )
        .where(Transcript.source == "asr", Transcript.model_key == model)
        .ext(distinct_on(Transcript.recording_id))
        .order_by(Transcript.recording_id, Transcript.created_at.desc(), Transcript.id.desc())
        .subquery()
    )


def select_items(db: Session, config: QcConfig, filters: Filters) -> list[Item]:
    latest = _latest_transcripts(config.model)
    query = (
        select(
            Recording.id, Recording.participant_id, Recording.duration_s, Recording.active_seconds,
            Recording.mean_dbfs, Recording.snr_db, Recording.wav_path,
            Assignment.prompt_type, Assignment.prompt_id, Assignment.prompt_text_snapshot,
            latest.c.transcript_id, latest.c.text, latest.c.avg_logprob,
        )
        .join(latest, latest.c.recording_id == Recording.id)
        .join(Assignment, Assignment.id == Recording.assignment_id)  # unassigned: not scored
        .join(Participant, Participant.participant_id == Recording.participant_id)
        .where(Recording.status == "stored", Recording.qc_status != "rejected")
        .order_by(Recording.id)
    )
    if not filters.include_test:
        query = query.where(Participant.is_test.is_(False))
    if filters.recording_ids:
        query = query.where(Recording.id.in_(filters.recording_ids))
    if filters.participants:
        query = query.where(Recording.participant_id.in_(filters.participants))
    if filters.since:
        start = datetime(filters.since.year, filters.since.month, filters.since.day, tzinfo=LOCAL_TZ)
        query = query.where(Recording.created_at >= start)
    if filters.batch_id is not None:
        query = query.where(
            exists().where(Transcript.recording_id == Recording.id, Transcript.batch_id == filters.batch_id)
        )
    if not filters.rescore:
        query = query.where(
            ~exists().where(
                QcResult.recording_id == Recording.id, QcResult.thresholds_version == config.thresholds_version
            )
        )
    if filters.limit is not None:
        query = query.limit(filters.limit)
    return [
        Item(
            recording_id=row.id, participant_id=row.participant_id, task_type=row.prompt_type,
            prompt_id=row.prompt_id, prompt_text=row.prompt_text_snapshot, transcript_id=row.transcript_id,
            transcript=row.text, avg_logprob=row.avg_logprob, duration_s=row.duration_s,
            active_seconds=row.active_seconds, mean_dbfs=row.mean_dbfs, snr_db=row.snr_db, wav_path=row.wav_path,
        )
        for row in db.execute(query)
    ]


# =======
# SCORING
# =======
@dataclass
class Scored:
    """One recording's metrics and level, as written (or, in a dry run, as it would be)."""

    item: Item
    metrics: dict[str, Any]
    band: int | None  # None: no level for this task type ("not_applicable")
    band_reason: str
    new_snr_db: float | None = None  # computed this run (snr_db was missing)

    @property
    def report_row(self) -> "ReportRow":
        return ReportRow(
            recording_id=self.item.recording_id, participant_id=self.item.participant_id,
            task_type=self.item.task_type, prompt_id=self.item.prompt_id, wav_path=self.item.wav_path,
            band=self.band, band_reason=self.band_reason, metrics=self.metrics,
        )  # fmt: skip


@dataclass
class ReportRow:
    recording_id: int
    participant_id: str
    task_type: str
    prompt_id: str | None
    wav_path: str | None
    band: int | None
    band_reason: str
    metrics: dict[str, Any] = field(default_factory=dict)


def _snr(item: Item, storage: FileStorage) -> float | None:
    """WADA-SNR of the recording's WAV; None (and logged by ID) if it can't be read."""
    if not item.wav_path or not storage.exists(item.wav_path):
        logger.warning("Recording %s: WAV missing, no SNR", item.recording_id)
        return None
    try:
        return m.wada_snr(m.read_wav(storage.absolute_path(item.wav_path)))
    except Exception as exc:  # noqa: BLE001 - one unreadable WAV mustn't stop the run
        logger.warning("Recording %s: SNR not computed (%s)", item.recording_id, type(exc).__name__)
        return None


def score_item(item: Item, config: QcConfig, storage: FileStorage) -> Scored:
    new_snr = _snr(item, storage) if item.snr_db is None else None
    snr = item.snr_db if item.snr_db is not None else new_snr
    words = m.word_count(item.transcript)
    values: dict[str, Any] = {
        "duration_s": item.duration_s,
        "speech_ratio": m.speech_ratio(item.active_seconds, item.duration_s),
        "mean_dbfs": item.mean_dbfs,
        "snr_db": snr,
        "word_count": words,
        "words_per_second": m.words_per_second(words, item.active_seconds),
        "avg_logprob": item.avg_logprob,
        "compression_ratio": m.compression_ratio(item.transcript),
        "cer": None,
        "wer": None,
        "length_ratio": None,
        "has_digits": None,
    }
    if item.task_type == "sentence":
        values.update(
            cer=m.cer(item.prompt_text, item.transcript),
            wer=m.wer(item.prompt_text, item.transcript),
            length_ratio=m.length_ratio(item.prompt_text, item.transcript),
            has_digits=m.has_digits(item.prompt_text),
        )
    band, reason = clip_level(values, item.task_type, config)
    return Scored(item, values, band, reason, new_snr)


def write_scored(db: Session, scored: Scored, config: QcConfig, now: datetime) -> QcResult:
    """snr_db (if new), the qc_results row and qc_tier2, in the caller's transaction.
    updated_at is not set here (and snr_db leaves it as it was); the recordings trigger bumps
    it when qc_tier2 (and so possibly qc_final) actually changes, which is what the supervision
    gateway's incremental sync reads."""
    item = scored.item
    if scored.new_snr_db is not None:
        db.execute(
            update(Recording)
            .where(Recording.id == item.recording_id, Recording.snr_db.is_(None))
            .values(snr_db=scored.new_snr_db, updated_at=Recording.updated_at)
        )
    row = QcResult(
        recording_id=item.recording_id,
        transcript_id=item.transcript_id,
        stage=STAGE,
        word_count=scored.metrics["word_count"],
        words_per_second=scored.metrics["words_per_second"],
        compression_ratio=scored.metrics["compression_ratio"],
        cer=scored.metrics["cer"],
        wer=scored.metrics["wer"],
        length_ratio=scored.metrics["length_ratio"],
        has_digits=scored.metrics["has_digits"],
        band=scored.band,
        band_reason=scored.band_reason,
        metrics_version=config.metrics_version,
        thresholds_version=config.thresholds_version,
        created_at=now,
    )
    db.add(row)
    db.flush()
    db.execute(
        update(Recording)
        .where(Recording.id == item.recording_id)
        .values(qc_tier2=scored.band, updated_at=Recording.updated_at)
    )
    return row


@dataclass
class RunResult:
    scored: list[Scored] = field(default_factory=list)
    failed_ids: list[int] = field(default_factory=list)
    dry_run: bool = False

    @property
    def rows(self) -> list[ReportRow]:
        return [s.report_row for s in self.scored]


def run_scoring(
    session_factory: sessionmaker[Session],
    config: QcConfig,
    filters: Filters,
    dry_run: bool = False,
    storage: FileStorage | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RunResult:
    """Score the selected recordings, each in its own transaction (a failure leaves that
    recording unscored and the run goes on). A dry run computes everything and writes nothing.
    Call under scoring_lock()."""
    storage = storage or get_audio_storage()
    with session_factory() as db:
        items = select_items(db, config, filters)
    result = RunResult(dry_run=dry_run)
    for item in items:
        try:
            scored = score_item(item, config, storage)
            if not dry_run:
                with session_factory() as db:
                    write_scored(db, scored, config, now())
                    db.commit()
            result.scored.append(scored)
        except Exception as exc:  # noqa: BLE001 - reported by recording ID; the next run retries
            logger.error("Recording %s not scored (%s)", item.recording_id, type(exc).__name__)
            result.failed_ids.append(item.recording_id)
    logger.info(
        "Tier-2 scoring (%s)%s: %s scored, %s failed",
        config.thresholds_version, " DRY RUN" if dry_run else "", len(result.scored), len(result.failed_ids),
    )  # fmt: skip
    return result


def count_untranscribed(db: Session, config: QcConfig, filters: Filters) -> int:
    """Eligible recordings (stored, not rejected, assigned, not test unless include_test) with
    no transcript from the configured model yet: waiting for the ASR batch."""
    has_transcript = exists().where(
        Transcript.recording_id == Recording.id, Transcript.source == "asr", Transcript.model_key == config.model
    )
    query = (
        select(func.count(Recording.id))
        .join(Assignment, Assignment.id == Recording.assignment_id)
        .join(Participant, Participant.participant_id == Recording.participant_id)
        .where(Recording.status == "stored", Recording.qc_status != "rejected", ~has_transcript)
    )
    if not filters.include_test:
        query = query.where(Participant.is_test.is_(False))
    if filters.recording_ids:
        query = query.where(Recording.id.in_(filters.recording_ids))
    if filters.participants:
        query = query.where(Recording.participant_id.in_(filters.participants))
    if filters.since:
        start = datetime(filters.since.year, filters.since.month, filters.since.day, tzinfo=LOCAL_TZ)
        query = query.where(Recording.created_at >= start)
    return db.scalar(query) or 0


# ===============
# AFTER ASR BATCH
# ===============
def score_after_batch(session_factory: sessionmaker[Session], batch_id: int) -> RunResult | None:
    """Score the recordings the ASR batch transcribed (new rows even if already scored: the
    transcript is new). Test participants only if the batch included them. Skipped (None) if
    another scoring run holds the lock: the next run picks the clips up."""
    config = load_qc_config()
    with session_factory() as db:
        batch = db.get(AsrBatch, batch_id)
        include_test = bool(((batch.filters if batch else None) or {}).get("include_test"))
    bind = session_factory.kw.get("bind")
    engine = bind if isinstance(bind, Engine) else get_sync_engine()
    try:
        with scoring_lock(engine):
            return run_scoring(
                session_factory, config, Filters(batch_id=batch_id, rescore=True, include_test=include_test)
            )
    except ScoringLocked:
        logger.info("Batch %s: tier-2 scoring skipped, another scoring run is in progress", batch_id)
        return None


# ==============
# STORED RESULTS
# ==============
def stored_rows(db: Session, config: QcConfig, filters: Filters) -> list[ReportRow]:
    """The latest result per recording for the current thresholds_version (created_at, then
    id), for the report and the listening sample."""
    latest = (
        select(QcResult)
        .where(QcResult.thresholds_version == config.thresholds_version)
        .ext(distinct_on(QcResult.recording_id))
        .order_by(QcResult.recording_id, QcResult.created_at.desc(), QcResult.id.desc())
        .subquery()
    )
    query = (
        select(
            latest, Recording.participant_id, Recording.wav_path, Recording.duration_s,
            Recording.active_seconds, Recording.mean_dbfs, Recording.snr_db,
            Assignment.prompt_type, Assignment.prompt_id, Transcript.avg_logprob,
        )
        .join(Recording, Recording.id == latest.c.recording_id)
        .join(Assignment, Assignment.id == Recording.assignment_id)
        .join(Participant, Participant.participant_id == Recording.participant_id)
        .join(Transcript, Transcript.id == latest.c.transcript_id)
        .order_by(latest.c.recording_id)
    )
    if not filters.include_test:
        query = query.where(Participant.is_test.is_(False))
    if filters.recording_ids:
        query = query.where(Recording.id.in_(filters.recording_ids))
    if filters.participants:
        query = query.where(Recording.participant_id.in_(filters.participants))
    if filters.since:
        start = datetime(filters.since.year, filters.since.month, filters.since.day, tzinfo=LOCAL_TZ)
        query = query.where(Recording.created_at >= start)
    rows = []
    for row in db.execute(query):
        metrics = {
            "duration_s": row.duration_s,
            "speech_ratio": m.speech_ratio(row.active_seconds, row.duration_s),
            "mean_dbfs": row.mean_dbfs,
            "snr_db": row.snr_db,
            "word_count": row.word_count,
            "words_per_second": row.words_per_second,
            "avg_logprob": row.avg_logprob,
            "compression_ratio": row.compression_ratio,
            "cer": row.cer,
            "wer": row.wer,
            "length_ratio": row.length_ratio,
            "has_digits": row.has_digits,
        }
        rows.append(
            ReportRow(row.recording_id, row.participant_id, row.prompt_type, row.prompt_id, row.wav_path,
                      row.band, row.band_reason, metrics)
        )  # fmt: skip
    return rows
