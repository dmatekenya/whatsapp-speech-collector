import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    FetchedValue,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy import text as sql_text  # Prompt has a column named "text"
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

STATUSES = ("received", "queued", "processed", "failed", "unregistered")
RECORDING_STATUSES = ("pending", "stored", "failed")
QC_STATUSES = ("pending", "accepted", "rejected", "needs_review")
# Tier 3 (supervisors) and the final label: docs/methodology.md 8.4-8.5; the rules are in the
# database function qc_final_label() (migration d9f4b2c8e631).
QC_TIER3_DECISIONS = ("accept", "reject")
QC_TIER3_REASONS = ("wrong_sentence", "no_speech", "noise", "unclear", "not_own_voice", "not_chichewa", "other")
QC_FINAL_LABELS = ("bronze", "silver", "gold", "rejected")
SESSION_STATUSES = ("active", "completed", "paused", "abandoned")
ASSIGNMENT_STATUSES = ("sent", "recorded", "accepted", "rejected", "expired", "skipped")
# "inactive" is kept for rows imported before paused/completed existed.
PARTICIPANT_STATUSES = ("active", "inactive", "paused", "completed", "withdrawn")
CONVERSATION_STATES = ("idle", "awaiting_ready", "in_session", "awaiting_continue", "awaiting_stop_confirm")
# skipped_window: not sent (or refused) because the participant's last message
# on that channel is older than WhatsApp's 24-hour window.
OUTBOUND_STATUSES = ("pending", "sent", "failed", "delivered", "read", "skipped_window")
CHANNELS = ("twilio", "meta")
FORWARDED_VALUES = ("no", "forwarded", "frequently_forwarded")
# Where a voice note came from (from Meta's voice and forwarded flags; Twilio: unknown).
AUDIO_ORIGINS = ("recorded", "forwarded", "uploaded", "unknown")
# Longer than the task type's flag_over (config clip_limits): kept and counted, split before training.
LENGTH_FLAGS = ("long_sentence", "long_clip")
ASR_BATCH_STATUSES = ("running", "completed", "partial", "failed", "interrupted")
ASR_BATCH_TRIGGERS = ("scheduled", "manual")
# asr: a model's output (written by the ASR batch); human and prompt come later.
TRANSCRIPT_SOURCES = ("asr", "human", "prompt")


def _in_check(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


class Base(DeclarativeBase):
    pass


# ========================
# REGISTRY AND PROMPT BANK
# ========================
class Participant(Base):
    """A registered speaker.

    RESTRICTED: this is the only table holding participants' names and phone
    numbers. Everything else refers to participants by ``participant_id``
    (P0001, ...). Limit access to it, and never log or export ``name`` or
    ``phone_number``.
    """

    __tablename__ = "participants"
    __table_args__ = (
        CheckConstraint(_in_check("status", PARTICIPANT_STATUSES), name="ck_participants_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    participant_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    phone_number: Mapped[str] = mapped_column(Text, unique=True, nullable=False)  # E.164
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    is_test: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sql_text("false")
    )
    batch: Mapped[str] = mapped_column(Text, nullable=False)
    enrolled_on: Mapped[date] = mapped_column(Date, nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sql_text("'{}'::jsonb")
    )
    source_file: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Prompt(Base):
    """A prompt shown to participants: text to read, a question, or an image.

    Text prompts have text/normalized_text (duplicates detected on
    normalized_text); image prompts have media_path/content_sha256 (duplicates
    detected on the file's SHA-256). Which columns are required per type is
    enforced by the importer config (config/prompts.yaml).
    """

    __tablename__ = "prompts"
    __table_args__ = (
        UniqueConstraint("prompt_type", "normalized_text", name="uq_prompts_type_normalized_text"),
        UniqueConstraint("prompt_type", "content_sha256", name="uq_prompts_type_content_sha256"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    prompt_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    prompt_type: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    normalized_text: Mapped[str | None] = mapped_column(Text)
    # Media prompts: path relative to Settings.prompt_media_root.
    media_path: Mapped[str | None] = mapped_column(Text)
    media_content_type: Mapped[str | None] = mapped_column(Text)
    # The prepared file's size and pixel dimensions.
    media_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    media_width: Mapped[int | None]
    media_height: Mapped[int | None]
    # SHA-256 of the original file as supplied (not the prepared copy), so
    # re-importing the same original is detected as a duplicate.
    content_sha256: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str | None] = mapped_column(Text, index=True)  # optional for some types
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sql_text("'{}'::jsonb")
    )
    # Soft-rule results from import, e.g. {"length": "long"}. Flagged prompts
    # are still imported and active.
    flags: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sql_text("'{}'::jsonb")
    )
    source_file: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sql_text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# =======================
# MESSAGES AND RECORDINGS
# =======================
class InboundMessage(Base):
    """Every inbound WhatsApp message, stored as received from the channel.

    RESTRICTED: this table contains participants' phone numbers (``sender`` and
    inside ``raw_payload``). Limit access to it, and never log or export these
    columns without masking.
    """

    __tablename__ = "inbound_messages"
    __table_args__ = (
        CheckConstraint(
            _in_check("status", STATUSES),
            name="ck_inbound_messages_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_sid: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    # Which webhook it arrived on ("twilio" or "meta"); replies go out on the same channel.
    channel: Mapped[str] = mapped_column(Text, nullable=False, server_default="twilio")
    sender: Mapped[str] = mapped_column(Text, nullable=False)  # E.164
    message_type: Mapped[str] = mapped_column(Text, nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Set by the worker once the sender is matched to an active participant.
    participant_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("participants.participant_id", name="fk_inbound_messages_participant_id"),
        index=True,
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="received", index=True
    )
    attempts: Mapped[int] = mapped_column(nullable=False, server_default=sql_text("0"))
    error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the message's media was deleted from the channel (Twilio only): after
    # it was stored as a recording, or without downloading it for senders
    # who aren't registered. Media can't be downloaded after this.
    media_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Recording(Base):
    """One stored voice note: the original file plus a 16 kHz mono WAV.

    Paths are relative to Settings.audio_root so the root can move. Folder and
    file names never contain phone numbers or names.
    """

    __tablename__ = "recordings"
    __table_args__ = (
        CheckConstraint(_in_check("status", RECORDING_STATUSES), name="ck_recordings_status"),
        CheckConstraint(_in_check("qc_status", QC_STATUSES), name="ck_recordings_qc_status"),
        CheckConstraint(_in_check("forwarded", FORWARDED_VALUES), name="ck_recordings_forwarded"),
        CheckConstraint(_in_check("audio_origin", AUDIO_ORIGINS), name="ck_recordings_audio_origin"),
        CheckConstraint(_in_check("length_flag", LENGTH_FLAGS), name="ck_recordings_length_flag"),
        CheckConstraint("qc_tier2 BETWEEN 1 AND 4", name="ck_recordings_qc_tier2"),
        CheckConstraint(_in_check("qc_tier3", QC_TIER3_DECISIONS), name="ck_recordings_qc_tier3"),
        CheckConstraint(_in_check("qc_final", QC_FINAL_LABELS), name="ck_recordings_qc_final"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    inbound_message_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("inbound_messages.id"), unique=True, nullable=False
    )
    participant_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("participants.participant_id", name="fk_recordings_participant_id"),
        index=True,
    )
    # The assignment this answers (a prompt, or a free_speech invitation). None:
    # an unassigned recording, sent outside a session; kept, never counted.
    assignment_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("assignments.id", name="fk_recordings_assignment_id", use_alter=True),
        index=True,
    )
    folder: Mapped[str | None] = mapped_column(Text)
    original_path: Mapped[str | None] = mapped_column(Text)
    wav_path: Mapped[str | None] = mapped_column(Text)
    original_content_type: Mapped[str | None] = mapped_column(Text)
    duration_s: Mapped[float | None] = mapped_column(Float)
    sample_rate: Mapped[int | None]
    channels: Mapped[int | None]
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)  # WAV file size
    # Measured on the WAV (src/audio/levels.py, settings in config clip_checks):
    # seconds in frames above the activity threshold, and the overall RMS level.
    # None: not measured (older rows until backfilled, or the measurement failed).
    active_seconds: Mapped[float | None] = mapped_column(Float)
    mean_dbfs: Mapped[float | None] = mapped_column(Float)
    # SHA-256 of the original bytes as received. Not unique: exact duplicates
    # (forwarded or re-sent voice notes) are stored and only flagged.
    content_sha256: Mapped[str | None] = mapped_column(Text, index=True)
    duplicate_of_recording_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("recordings.id", name="fk_recordings_duplicate_of_recording_id")
    )
    # Media removed from the provider after safe storage (Twilio only; Meta
    # keeps received media for a limited time and needs no deletion).
    provider_media_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sql_text("false")
    )
    # Meta only (None for Twilio, which doesn't say): recorded in WhatsApp as
    # a voice note (True) or an uploaded audio file (False).
    is_voice_note: Mapped[bool | None] = mapped_column(Boolean)
    # Meta only: "no", "forwarded" or "frequently_forwarded" (None for Twilio).
    forwarded: Mapped[str | None] = mapped_column(Text)
    # recorded | forwarded | uploaded | unknown (from the two above; Twilio: unknown).
    # config audio_origin decides what happens to each (accept, reject, accept_flagged).
    audio_origin: Mapped[str] = mapped_column(Text, nullable=False, server_default="unknown")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    error: Mapped[str | None] = mapped_column(Text)
    # Quality: storage status above says the file is safe; this says whether
    # the clip is usable. The conversation engine rejects clips that are
    # empty, too short, too long or have no speech, and flags low_speech;
    # the QC step will set the rest.
    qc_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending", index=True)
    qc_reason: Mapped[str | None] = mapped_column(Text)  # e.g. too_short, no_speech, low_speech
    # long_sentence | long_clip: longer than flag_over in config clip_limits (by task type;
    # default outside a session). Information only, never a review flag: such clips are split
    # before training. None: within flag_over (or not measured).
    length_flag: Mapped[str | None] = mapped_column(Text)
    # WADA-SNR estimate of the WAV (dB), set by tier-2 scoring (src/qc); an audio fact, computed once.
    snr_db: Mapped[float | None] = mapped_column(Float)
    # Tier-2 level 1-4: a copy of the latest qc_results.band (None: not scored). Tier 2
    # never changes qc_status / qc_reason (tier 1).
    qc_tier2: Mapped[int | None] = mapped_column(SmallInteger)
    # Tier 3: the latest supervisor decision (accept / reject) and its reason, copied from
    # qc_human_reviews by a trigger. Never written by the app.
    qc_tier3: Mapped[str | None] = mapped_column(Text)
    qc_tier3_reason: Mapped[str | None] = mapped_column(Text)
    # Final label (bronze / silver / gold / rejected), set by the recordings trigger from
    # qc_final_label() whenever qc_status, qc_tier2, qc_tier3 or assignment_id changes.
    # Never written by the app (FetchedValue: the database fills it).
    qc_final: Mapped[str] = mapped_column(Text, nullable=False, server_default=FetchedValue())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


# =========
# TASK BANK
# =========
class TaskSession(Base):
    """A run of prompts of one task type sent to one participant (table "sessions")."""

    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint(_in_check("status", SESSION_STATUSES), name="ck_sessions_status"),
        # At most one active session per participant.
        Index(
            "uq_sessions_one_active_per_participant",
            "participant_id",
            unique=True,
            postgresql_where=sql_text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    participant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("participants.participant_id"), nullable=False, index=True
    )
    task_type: Mapped[str] = mapped_column(Text, nullable=False)  # a config task type
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    size: Mapped[int] = mapped_column(nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Assignment(Base):
    """One prompt given to one participant, and what happened to it.

    A participant never gets the same prompt twice. A prompt's coverage is the
    number of its assignments that are accepted, recorded, or sent and not yet
    expired; see src/tasks/bank.py.
    """

    __tablename__ = "assignments"
    __table_args__ = (
        CheckConstraint(_in_check("status", ASSIGNMENT_STATUSES), name="ck_assignments_status"),
        UniqueConstraint("participant_id", "prompt_id", name="uq_assignments_participant_prompt"),
        UniqueConstraint("session_id", "position", name="uq_assignments_session_position"),
        Index("ix_assignments_prompt_status", "prompt_id", "status"),
        Index("ix_assignments_participant_status", "participant_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    participant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("participants.participant_id"), nullable=False
    )
    # None for unprompted task types (free_speech): an invitation to talk, not a prompt.
    prompt_id: Mapped[str | None] = mapped_column(Text, ForeignKey("prompts.prompt_id"))
    prompt_type: Mapped[str] = mapped_column(Text, nullable=False)
    session_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sessions.id"), nullable=False)
    position: Mapped[int] = mapped_column(nullable=False)  # 1..session size
    # The text exactly as sent (prompts can change later); None for images.
    prompt_text_snapshot: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="sent")
    outbound_message_sid: Mapped[str | None] = mapped_column(Text, index=True)
    retake_count: Mapped[int] = mapped_column(nullable=False, server_default=sql_text("0"))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recording_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("recordings.id", name="fk_assignments_recording_id")
    )
    # The caption/wrapper variant used when the prompt was sent (index into
    # the message catalog's list, e.g. prompt.image_caption); None for single texts.
    message_variant: Mapped[int | None]


# ============
# CONVERSATION
# ============
class ConversationState(Base):
    """Where each participant is in the conversation with the bot."""

    __tablename__ = "conversation_state"
    __table_args__ = (
        CheckConstraint(_in_check("state", CONVERSATION_STATES), name="ck_conversation_state_state"),
    )

    participant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("participants.participant_id"), primary_key=True
    )
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="idle")
    # e.g. {"offered_task_type": "image", "previous_state": "in_session",
    #       "welcomed": true, "instructions_seen": {"general": true, "image": true}}
    context: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sql_text("'{}'::jsonb")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class OutboundMessage(Base):
    """Every message the bot sends, written in the same transaction as the
    state change it belongs to and sent afterwards by the send_outbound job.

    RESTRICTED: bodies can contain participants' first names. The recipient's
    number is never stored here; it comes from the inbound message or registry.
    """

    __tablename__ = "outbound_messages"
    __table_args__ = (
        CheckConstraint(_in_check("status", OUTBOUND_STATUSES), name="ck_outbound_messages_status"),
        # A retried job can never send the same reply twice.
        UniqueConstraint("inbound_message_id", "purpose", name="uq_outbound_messages_inbound_purpose"),
        UniqueConstraint("channel", "provider_message_id", name="uq_outbound_messages_channel_provider_message_id"),
        CheckConstraint(_in_check("channel", CHANNELS), name="ck_outbound_messages_channel"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    participant_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("participants.participant_id"), index=True
    )
    # The message being answered.
    inbound_message_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("inbound_messages.id"), index=True
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)  # "reply", "reply_media"
    # Order within one turn: a long text goes before its image.
    sequence: Mapped[int] = mapped_column(nullable=False, server_default=sql_text("1"))
    # [{"key": "welcome.first_time", "variant": null}, {"key": "prompt.image_caption", "variant": 2}]
    message_keys: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=sql_text("'[]'::jsonb")
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    media_prompt_id: Mapped[str | None] = mapped_column(Text, ForeignKey("prompts.prompt_id"))
    # The prompt this message carries, so the send job can mark it sent.
    assignment_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("assignments.id"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    # The channel it is sent on: the inbound message's channel ("twilio" or "meta").
    channel: Mapped[str] = mapped_column(Text, nullable=False, server_default="twilio")
    # The provider's ID once sent: Twilio's SM... or Meta's wamid....
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[int | None]
    # The provider's error fields: code, title, subcode, fbtrace_id (Meta) or
    # code (Twilio). Never tokens, phone numbers or message text.
    error_details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # The conversation state before and after the turn that wrote this reply:
    # {"before": {"state", "context"}, "after": {...}}. If the reply isn't
    # delivered, the state goes back to "before" (unless a later turn moved on).
    state_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ===
# ASR
# ===
class AsrBatch(Base):
    """One run of the ASR batch (scripts/asr_batch.py). Nothing personal: recording IDs live in transcripts."""

    __tablename__ = "asr_batches"
    __table_args__ = (
        CheckConstraint(_in_check("status", ASR_BATCH_STATUSES), name="ck_asr_batches_status"),
        CheckConstraint(_in_check("trigger", ASR_BATCH_TRIGGERS), name="ck_asr_batches_trigger"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="running")
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    model_key: Mapped[str] = mapped_column(Text, nullable=False)
    # As Modal reports them once a chunk has come back: the repo and its resolved commit SHA.
    model_repo: Mapped[str | None] = mapped_column(Text)
    model_revision: Mapped[str | None] = mapped_column(Text)
    # The options used: dates, participant IDs, recording IDs, include_test, force, limit.
    filters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sql_text("'{}'::jsonb")
    )
    clips_selected: Mapped[int] = mapped_column(nullable=False, server_default=sql_text("0"))
    clips_transcribed: Mapped[int] = mapped_column(nullable=False, server_default=sql_text("0"))
    clips_failed: Mapped[int] = mapped_column(nullable=False, server_default=sql_text("0"))
    # Selected but their WAV file isn't on disk (not sent, not counted in clips_selected).
    clips_missing: Mapped[int] = mapped_column(nullable=False, server_default=sql_text("0"))
    audio_seconds: Mapped[float] = mapped_column(Float, nullable=False, server_default=sql_text("0"))
    gpu: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)


class Transcript(Base):
    """A transcript of a recording. Several per recording over time (one per ASR model
    and run, later human); the latest per source and model is by created_at."""

    __tablename__ = "transcripts"
    __table_args__ = (
        CheckConstraint(_in_check("source", TRANSCRIPT_SOURCES), name="ck_transcripts_source"),
        Index("ix_transcripts_recording_source_model", "recording_id", "source", "model_key"),
        # Lets qc_results reference (transcript, recording) as a pair (id alone is already unique).
        UniqueConstraint("id", "recording_id", name="uq_transcripts_id_recording_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    recording_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("recordings.id"), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    # ASR only (None for human and prompt transcripts).
    model_key: Mapped[str | None] = mapped_column(Text)
    model_repo: Mapped[str | None] = mapped_column(Text)
    model_revision: Mapped[str | None] = mapped_column(Text)
    # As the model returned it (stripped, not normalised).
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # Mean log-probability of the generated tokens (ASR only).
    avg_logprob: Mapped[float | None] = mapped_column(Float)
    # ASR only: True if decoded with Whisper long-form generation (audio over 30 s);
    # None for older rows and other sources.
    long_form: Mapped[bool | None] = mapped_column(Boolean)
    batch_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("asr_batches.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# =========
# QC TIER 2
# =========
class QcResult(Base):
    """One tier-2 scoring run of one recording (src/qc): the transcript-based metrics, the
    level (band 1-4) and why. Rows are only added, never updated or deleted by the scorer;
    the latest per recording is by created_at, then id. Audio facts that don't change
    (duration_s, active_seconds, mean_dbfs, snr_db) stay on recordings, and avg_logprob on
    the transcript."""

    __tablename__ = "qc_results"
    __table_args__ = (
        # A result can only pair a recording with one of its own transcripts.
        ForeignKeyConstraint(
            ["transcript_id", "recording_id"],
            ["transcripts.id", "transcripts.recording_id"],
            name="fk_qc_results_transcript_recording",
            ondelete="RESTRICT",
        ),
        CheckConstraint("band IS NULL OR band BETWEEN 1 AND 4", name="ck_qc_results_band"),
        # No level exactly when the clip's task type gets none (or no level-setting metric applied).
        CheckConstraint(
            "(band IS NULL) = (band_reason = 'not_applicable')", name="ck_qc_results_band_not_applicable"
        ),
        Index("ix_qc_results_recording_created_id", "recording_id", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    recording_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("recordings.id", ondelete="RESTRICT", name="fk_qc_results_recording_id"), nullable=False
    )
    transcript_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False, server_default="asr")
    word_count: Mapped[int | None]
    words_per_second: Mapped[float | None] = mapped_column(Float)
    compression_ratio: Mapped[float | None] = mapped_column(Float)
    # Sentences only (None for other task types).
    cer: Mapped[float | None] = mapped_column(Float)
    wer: Mapped[float | None] = mapped_column(Float)
    length_ratio: Mapped[float | None] = mapped_column(Float)
    has_digits: Mapped[bool | None] = mapped_column(Boolean)
    # The level 1-4; None ("not_applicable") for task types that get no level (since
    # 2026-11-11 only sentences get one).
    band: Mapped[int | None] = mapped_column(SmallInteger)
    # The metric(s) at the lowest level, e.g. "cer:2"; "all_ok" for 4; "not_applicable" without a level.
    band_reason: Mapped[str] = mapped_column(Text, nullable=False)
    metrics_version: Mapped[str] = mapped_column(Text, nullable=False)
    thresholds_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# =========
# QC TIER 3
# =========
class QcHumanReview(Base):
    """One supervisor decision on one recording (tier 3), inserted by the supervision gateway.

    Append-only: a trigger refuses UPDATE and DELETE (except setting notified_at once); a
    reversal is a new row, and the latest (created_at, then id) is the verdict. Another trigger
    copies each new decision to recordings.qc_tier3 / qc_tier3_reason, after checking the
    gateway's expected state (SQLSTATE QC409 on a mismatch). See migration d9f4b2c8e631.
    """

    __tablename__ = "qc_human_reviews"
    __table_args__ = (
        CheckConstraint(_in_check("decision", QC_TIER3_DECISIONS), name="ck_qc_human_reviews_decision"),
        CheckConstraint(_in_check("reason_code", QC_TIER3_REASONS), name="ck_qc_human_reviews_reason_code"),
        CheckConstraint("(decision = 'reject') = (reason_code IS NOT NULL)", name="ck_qc_human_reviews_reason_when_rejected"),
        CheckConstraint(
            "reason_code IS DISTINCT FROM 'other' OR btrim(coalesce(note, '')) <> ''",
            name="ck_qc_human_reviews_note_for_other",
        ),
        CheckConstraint("note IS NULL OR char_length(note) <= 500", name="ck_qc_human_reviews_note_length"),
        CheckConstraint("btrim(reviewer_id) <> '' AND btrim(reviewer_name) <> ''", name="ck_qc_human_reviews_reviewer"),
        UniqueConstraint("operation_key", name="uq_qc_human_reviews_operation_key"),
        Index("ix_qc_human_reviews_recording_created_id", "recording_id", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    recording_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("recordings.id", ondelete="RESTRICT", name="fk_qc_human_reviews_recording_id"),
        nullable=False,
    )
    decision: Mapped[str] = mapped_column(Text, nullable=False)  # accept, reject
    reason_code: Mapped[str | None] = mapped_column(Text)  # required exactly when rejected
    note: Mapped[str | None] = mapped_column(Text)  # required for reason "other"; at most 500 characters
    reviewer_id: Mapped[str] = mapped_column(Text, nullable=False)
    reviewer_name: Mapped[str] = mapped_column(Text, nullable=False)
    operation_key: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)  # the gateway's idempotency key
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # When the contributor was told about the decision (planned digest); set once.
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
