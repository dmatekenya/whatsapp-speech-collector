from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sql_text  # Prompt has a column named "text"
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

STATUSES = ("received", "queued", "processed", "failed", "unregistered")
RECORDING_STATUSES = ("pending", "stored", "failed")
QC_STATUSES = ("pending", "accepted", "rejected", "needs_review")
SESSION_STATUSES = ("active", "completed", "paused", "abandoned")
ASSIGNMENT_STATUSES = ("sent", "recorded", "accepted", "rejected", "expired", "skipped")
# "inactive" is kept for rows imported before paused/completed existed.
PARTICIPANT_STATUSES = ("active", "inactive", "paused", "completed", "withdrawn")
CONVERSATION_STATES = ("idle", "awaiting_ready", "in_session", "awaiting_continue", "awaiting_stop_confirm")
OUTBOUND_STATUSES = ("pending", "sent", "failed", "delivered", "read")


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
    # When the message's media was deleted from the channel (Twilio): after
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
    # The prompt this answers; None means free speech (not prompted).
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
    # SHA-256 of the original bytes as received. Not unique: exact duplicates
    # (forwarded or re-sent voice notes) are stored and only flagged.
    content_sha256: Mapped[str | None] = mapped_column(Text, index=True)
    duplicate_of_recording_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("recordings.id", name="fk_recordings_duplicate_of_recording_id")
    )
    twilio_media_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sql_text("false")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    error: Mapped[str | None] = mapped_column(Text)
    # Quality: storage status above says the file is safe; this says whether
    # the clip is usable. The conversation engine rejects clips that are
    # empty, too short or too long; the QC step will set the rest.
    qc_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending", index=True)
    qc_reason: Mapped[str | None] = mapped_column(Text)  # e.g. too_short, too_long, empty
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
    task_type: Mapped[str] = mapped_column(Text, nullable=False)  # a prompt_type
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
    prompt_id: Mapped[str] = mapped_column(Text, ForeignKey("prompts.prompt_id"), nullable=False)
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
    twilio_sid: Mapped[str | None] = mapped_column(Text, unique=True)
    error: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
