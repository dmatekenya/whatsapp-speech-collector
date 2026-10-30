import hashlib
import logging
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from rq import Retry
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from src.audio.convert import probe, to_wav
from src.channels.base import ChannelAdapter, MediaDeleteError
from src.channels.base import InboundMessage as ChannelMessage
from src.channels.factory import get_channel
from src.config import get_settings
from src.conversation.engine import respond
from src.conversation.outbound import enqueue_send
from src.logging_config import mask_phone, setup_logging
from src.registry.participants import find_active_participant
from src.storage.audio import (
    AudioStorage,
    extension_for,
    get_audio_storage,
    recording_filename,
    resolve_folder,
)
from src.storage.db import get_sync_sessionmaker
from src.storage.duplicates import relink_duplicates
from src.storage.models import InboundMessage, Recording
from src.storage.queue import get_inbound_queue

setup_logging()
logger = logging.getLogger(__name__)

AUDIO = "audio"
DELETE_MEDIA_JOB = "src.workers.jobs.delete_channel_media"
# Twilio can refuse right after a message arrives; keep trying for about a day.
DELETE_MEDIA_RETRY = Retry(max=5, interval=[60, 300, 1800, 7200, 21600])


class MediaGoneError(RuntimeError):
    """The message's media was deleted from the channel before it was stored."""


# =======
# HELPERS
# =======
def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:2000]


def _has_stored_recording(session: Session, message_id: int) -> bool:
    return (
        session.scalar(
            select(Recording.id).where(
                Recording.inbound_message_id == message_id, Recording.status == "stored"
            )
        )
        is not None
    )


# =====
# AUDIO
# =====
def _store_recording(
    session: Session,
    recording: Recording,
    message: InboundMessage,
    inbound: ChannelMessage,
    channel: ChannelAdapter,
    storage: AudioStorage,
) -> None:
    """Download, convert and store one voice note, then commit it as 'stored'."""
    participant_id = message.participant_id
    folder = resolve_folder(message.sender, participant_id)
    ext = extension_for(inbound.media_content_type)
    original_path = f"{folder}/{recording_filename(participant_id, recording.id, ext)}"
    wav_path = f"{folder}/{recording_filename(participant_id, recording.id, 'wav')}"

    try:
        data = channel.download_media(inbound)
        # Hash the original as received (not the WAV) to spot forwarded or
        # re-sent voice notes; Twilio doesn't pass WhatsApp's forwarded flag.
        content_sha256 = hashlib.sha256(data).hexdigest()
        # Convert in a scratch directory first, so storage only ever receives
        # finished files (works the same once storage is Spaces).
        with tempfile.TemporaryDirectory(prefix="speech-collector-") as scratch:
            scratch_original = Path(scratch) / f"original.{ext}"
            scratch_original.write_bytes(data)
            scratch_wav = Path(scratch) / "audio.wav"
            to_wav(scratch_original, scratch_wav)
            info = probe(scratch_wav)

            storage.save(original_path, data)
            wav_size = storage.save(wav_path, scratch_wav)
    except Exception as exc:
        for path in (original_path, wav_path):
            storage.delete(path)
        session.rollback()
        recording.status = "failed"
        recording.error = _error_text(exc)
        session.commit()
        raise

    recording.participant_id = participant_id
    recording.folder = folder
    recording.original_path = original_path
    recording.wav_path = wav_path
    recording.original_content_type = inbound.media_content_type
    recording.duration_s = info.duration_s
    recording.sample_rate = info.sample_rate
    recording.channels = info.channels
    recording.size_bytes = wav_size
    recording.content_sha256 = content_sha256
    recording.status = "stored"
    recording.error = None
    session.flush()
    # Only flagged for now; rejection and a reply come with the QC and
    # conversation steps. If this message arrived before copies that were
    # stored earlier, they get re-pointed at this recording.
    changes = relink_duplicates(session, content_sha256)
    session.commit()
    for change in changes:
        if change.new is not None:
            logger.warning(
                "Recording id=%s has identical audio to recording id=%s, received earlier"
                " (forwarded or re-sent?)",
                change.recording_id,
                change.new,
            )
    logger.info(
        "Stored recording id=%s folder=%s duration=%.2fs from=%s",
        recording.id,
        folder,
        info.duration_s,
        mask_phone(message.sender),
    )


def has_media(message: InboundMessage) -> bool:
    payload = message.raw_payload or {}
    return bool(payload.get("MediaUrl0")) or payload.get("NumMedia", "0") not in ("", "0")


def _try_delete_media(channel: ChannelAdapter, inbound: ChannelMessage, label: str) -> bool:
    """Ask the channel to delete the media; log why if it can't. Never raises."""
    try:
        channel.delete_media(inbound)
    except MediaDeleteError as exc:
        logger.warning("Could not delete channel media for %s: %s", label, exc)
        return False
    except Exception as exc:
        # Unexpected errors can embed media URLs: log only the type.
        logger.warning("Could not delete channel media for %s: %s", label, type(exc).__name__)
        return False
    return True


def record_media_deleted(
    session: Session, message: InboundMessage, recording: Recording | None
) -> None:
    message.media_deleted_at = datetime.now(UTC)
    if recording is not None:
        recording.twilio_media_deleted = True
    session.commit()


def _schedule_media_deletion(message: InboundMessage, label: str) -> None:
    try:
        get_inbound_queue().enqueue(DELETE_MEDIA_JOB, message.message_sid, retry=DELETE_MEDIA_RETRY)
    except Exception as exc:
        logger.error(
            "Could not schedule a media deletion retry for %s (%s); "
            "run scripts/delete_channel_media.py",
            label,
            type(exc).__name__,
        )
        return
    logger.info("Scheduled media deletion retries for %s", label)


def _delete_channel_media(
    session: Session,
    message: InboundMessage,
    recording: Recording | None,
    inbound: ChannelMessage,
    channel: ChannelAdapter,
) -> None:
    """Delete the message's media from the channel; never fails the job.

    Only called once the audio is safely stored, or for senders who aren't
    registered (nothing is ever downloaded for them). If the channel refuses,
    a retry job (delete_channel_media) is scheduled.
    """
    label = f"recording id={recording.id}" if recording is not None else f"message id={message.id}"
    if not get_settings().twilio_delete_media:
        logger.info("Channel media deletion is off (TWILIO_DELETE_MEDIA); keeping media for %s", label)
        return
    if message.media_deleted_at is not None or (recording is not None and recording.twilio_media_deleted):
        return
    if _try_delete_media(channel, inbound, label):
        record_media_deleted(session, message, recording)
        logger.info("Deleted channel media for %s", label)
    else:
        _schedule_media_deletion(message, label)


def _process_audio(session: Session, message: InboundMessage) -> None:
    channel = get_channel()
    inbound = channel.parse_inbound(message.raw_payload)

    # One recording per message: create it or reuse the existing row. Commit
    # the pending row so a later rollback can't remove it, then lock it so
    # concurrent runs for the same message wait for each other.
    session.execute(
        insert(Recording)
        .values(inbound_message_id=message.id)
        .on_conflict_do_nothing(index_elements=[Recording.inbound_message_id])
    )
    session.commit()
    recording = session.scalars(
        select(Recording).where(Recording.inbound_message_id == message.id).with_for_update()
    ).one()

    if recording.status == "stored":
        logger.info("Recording id=%s already stored, not downloading again", recording.id)
    elif message.media_deleted_at is not None:
        raise MediaGoneError(f"Media for message id={message.id} was deleted before it was stored")
    else:
        _store_recording(session, recording, message, inbound, channel, get_audio_storage())

    # Only after the recording is committed as stored.
    _delete_channel_media(session, message, recording, inbound, channel)


# ====
# JOBS
# ====
def process_inbound(message_sid: str) -> None:
    """Process one stored inbound message. Safe to run more than once."""
    with get_sync_sessionmaker()() as session:
        message = session.scalar(
            select(InboundMessage).where(InboundMessage.message_sid == message_sid)
        )
        if message is None:
            raise LookupError(f"Inbound message {message_sid} not found")
        is_audio = message.message_type == AUDIO
        if message.status == "processed" and (
            not is_audio or _has_stored_recording(session, message.id)
        ):
            logger.info("Inbound message id=%s already processed, skipping", message.id)
            return

        # Commit the attempt up front so it counts even if the job crashes.
        message.attempts += 1
        session.commit()
        logger.info(
            "Processing inbound message id=%s type=%s from=%s attempt=%s",
            message.id,
            message.message_type,
            mask_phone(message.sender),
            message.attempts,
        )

        try:
            # Only active, registered participants are processed; nothing is
            # downloaded for anyone else. A re-run after they register (e.g.
            # via process_audio_backlog) picks the message up again.
            participant = find_active_participant(session, message.sender)
            if participant is None:
                message.status = "unregistered"
                message.participant_id = None
                message.error = None
                session.commit()
                logger.info(
                    "Inbound message id=%s from unregistered or inactive sender %s; not processed",
                    message.id,
                    mask_phone(message.sender),
                )
                if has_media(message):
                    # Never downloaded; delete it from the channel straight away.
                    channel = get_channel()
                    inbound = channel.parse_inbound(message.raw_payload)
                    _delete_channel_media(session, message, None, inbound, channel)
                _reply(message.message_sid)
                return
            message.participant_id = participant.participant_id

            if is_audio:
                _process_audio(session, message)
            # The engine sees the stored recording (duration, hash); it runs
            # before the message is marked processed so a failure is retried.
            session.commit()
            _reply(message.message_sid)
            message.status = "processed"
            message.processed_at = datetime.now(UTC)
            message.error = None
            session.commit()
        except Exception as exc:
            session.rollback()
            message.status = "failed"
            message.error = _error_text(exc)
            session.commit()
            logger.error(
                "Failed to process inbound message id=%s: %s", message.id, type(exc).__name__
            )
            if isinstance(exc, MediaGoneError):
                return  # retrying can't bring the media back
            # Re-raise so RQ records the failure and retries.
            raise


def _reply(message_sid: str) -> None:
    """Let the conversation engine answer, then start sending its reply."""
    pending = respond(message_sid)
    if pending:
        enqueue_send(pending[0])


def delete_channel_media(message_sid: str) -> None:
    """Retry job: delete a message's media from the channel. Raises so RQ retries.

    Deletes only when that's safe: the recording is stored, or the sender
    wasn't registered (their media is never downloaded).
    """
    if not get_settings().twilio_delete_media:
        logger.info("Channel media deletion is off (TWILIO_DELETE_MEDIA); not deleting")
        return
    with get_sync_sessionmaker()() as session:
        message = session.scalar(
            select(InboundMessage).where(InboundMessage.message_sid == message_sid).with_for_update()
        )
        if message is None:
            raise LookupError(f"Inbound message {message_sid} not found")
        if message.media_deleted_at is not None:
            return
        recording = session.scalar(
            select(Recording).where(Recording.inbound_message_id == message.id)
        )
        stored = recording is not None and recording.status == "stored"
        if not stored and message.status != "unregistered":
            logger.warning(
                "Not deleting media for message id=%s: it isn't stored yet", message.id
            )
            return
        label = f"recording id={recording.id}" if stored else f"message id={message.id}"
        channel = get_channel()
        if not _try_delete_media(channel, channel.parse_inbound(message.raw_payload), label):
            raise MediaDeleteError(f"Channel media for {label} not deleted yet; RQ will retry")
        record_media_deleted(session, message, recording if stored else None)
        logger.info("Deleted channel media for %s", label)
