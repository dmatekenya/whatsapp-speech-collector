import re
from functools import lru_cache

from src.config import get_settings
from src.storage.files import FileStorage, LocalFileStorage
from src.storage.models import Recording
from src.tasks.config import AUDIO_POLICY_REASONS, CollectionConfig, get_collection_config

UNASSIGNED_FOLDER = "_unassigned"
UNASSIGNED_PREFIX = "UNASSIGNED"

# Participant IDs end up in folder and file names: allow only short safe
# tokens, and reject anything that looks like a phone number.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PHONE_LIKE = re.compile(r"\d{7,}")

_EXTENSIONS = {
    "audio/ogg": "ogg",
    "audio/opus": "opus",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/aac": "aac",
    "audio/amr": "amr",
    "audio/webm": "webm",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
}


# ======
# NAMING
# ======
def _check_participant_id(participant_id: str) -> str:
    if not _SAFE_ID.match(participant_id) or _PHONE_LIKE.search(participant_id):
        raise ValueError("participant_id is not a safe folder/file name")
    return participant_id


def resolve_folder(sender: str, participant_id: str | None) -> str:
    """Folder for a participant's recordings. Never derived from the sender's number."""
    if participant_id:
        return _check_participant_id(participant_id)
    return UNASSIGNED_FOLDER


def recording_filename(participant_id: str | None, recording_id: int, ext: str) -> str:
    """The one place the file name format is defined, e.g. P0001_R000123.wav."""
    prefix = _check_participant_id(participant_id) if participant_id else UNASSIGNED_PREFIX
    return f"{prefix}_R{recording_id:06d}.{ext.lstrip('.')}"


def extension_for(content_type: str | None) -> str:
    base = (content_type or "").split(";")[0].strip().lower()
    return _EXTENSIONS.get(base, "bin")


# =======
# STORAGE
# =======
# The generic implementation lives in src/storage/files.py.
AudioStorage = FileStorage
LocalAudioStorage = LocalFileStorage


@lru_cache
def get_audio_storage() -> AudioStorage:
    return LocalAudioStorage(get_settings().audio_root)


# ============
# AUDIO ORIGIN
# ============
def apply_audio_origin(
    recording: Recording,
    origin: str,
    task_type: str | None = None,
    config: CollectionConfig | None = None,
) -> None:
    """Record where a voice note came from, and what config audio_origin says to do with it.

    reject (e.g. forwarded or uploaded): never used (QC rejected, not_recorded).
    accept_flagged (e.g. forwarded or uploaded in free_speech): used and
    counted, but marked for a speaker check (QC needs_review,
    speaker_check_pending). accept: as usual.

    The worker calls this without a task type (the global rule); the
    conversation engine calls it again with the task type of the prompt the
    clip answers, so a per-type override replaces what the worker set.
    """
    config = config or get_collection_config()
    recording.audio_origin = origin
    policy = config.audio_policy(origin, task_type)
    if policy in AUDIO_POLICY_REASONS:
        recording.qc_status, recording.qc_reason = AUDIO_POLICY_REASONS[policy]
    elif (recording.qc_status, recording.qc_reason) in AUDIO_POLICY_REASONS.values():
        # Set by an earlier call under a different rule: back to unchecked.
        recording.qc_status, recording.qc_reason = "pending", None


# ===========
# CLIP LENGTH
# ===========
def apply_length_flag(
    recording: Recording, task_type: str | None = None, config: CollectionConfig | None = None
) -> None:
    """recordings.length_flag from config clip_limits: long_sentence / long_clip when the clip
    is longer than flag_over for its task type, else None. Information only: never changes
    the QC status. The worker sets it with the default limits (no task type yet); the
    conversation engine sets it again once it knows the prompt the clip answers."""
    config = config or get_collection_config()
    recording.length_flag = config.clip_limits.length_flag(recording.duration_s, task_type)
